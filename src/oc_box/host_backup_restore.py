"""Read-only review and no-overwrite activation of a decoded host backup."""

import errno
import fcntl
import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import host_backup_payload as payload
    from . import log_config
    from .backup_crypto import BackupError
else:
    import host_backup_payload as payload
    import log_config
    from backup_crypto import BackupError


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_NATIVE = frozenset({payload.OPENCODE_CONFIG, payload.OPENCODE_AUTH})
_NATIVE_DB = ("opencode.db",)
_NATIVE_DB_LIMIT = 16 * 1024 * 1024
_LOCKS = {
    payload.OC_BOX_CONFIG: log_config._CONFIG_LOCK,
    payload.VAULT: ".vault.lock",
}
_DIAGNOSTICS = {
    "invalid-payload": "the decoded host payload is invalid",
    "unsafe-destination": "a private destination or its ancestry is unsafe",
    "workspace-overlap": "the workspace destination overlaps a private host scope",
    "occupied": "a destination already exists; nothing is overwritten",
    "destination-changed": "the reviewed destination changed",
    "locked": "a host settings or vault lock is busy",
    "atomic-unavailable": "anonymous, no-overwrite file publication is unavailable",
    "io-failure": "a host filesystem operation failed",
    "interrupted": "host file activation was interrupted",
}
HOST_FAILURE_CATEGORIES = frozenset(_DIAGNOSTICS)


@dataclass(frozen=True)
class RestoreResult:
    """Counts successful filesystem calls, not files rolled back on failure."""

    files_installed: int = 0
    directories_created: int = 0
    lock_files_created: int = 0
    completion_uncertain: bool = False


class HostRestoreError(BackupError):
    """Fixed diagnostic plus safe counts; no underlying errors or paths."""

    def __init__(self, category, result=None):
        if result is None:
            result = RestoreResult()
        self.category = category
        self.reason = "cancelled" if category == "interrupted" else None
        self.result = result
        message = (
            f"Host backup restore refused or incomplete: {_DIAGNOSTICS[category]}."
        )
        if (
            result.files_installed
            or result.directories_created
            or result.lock_files_created
            or result.completion_uncertain
        ):
            message += (
                f" Successful filesystem calls: {result.files_installed} file links,"
                f" {result.directories_created} directories,"
                f" {result.lock_files_created} lock files. No rollback was attempted."
            )
        if result.completion_uncertain:
            message += " The final location or durability of changes is unconfirmed."
        super().__init__(message)


class _Refusal(Exception):
    pass


@dataclass
class _Progress:
    files: int = 0
    directories: int = 0
    locks: int = 0
    uncertain: bool = False

    def result(self):
        return RestoreResult(self.files, self.directories, self.locks, self.uncertain)


@dataclass(frozen=True)
class _Entry:
    scope: str
    relative: tuple[str, ...] = field(repr=False)
    raw: bytes | None = field(repr=False)


@dataclass(frozen=True)
class RestorePlan:
    """An in-memory review binding. Contains private data; never serialize it."""

    _entries: tuple = field(repr=False)
    _roots: tuple = field(repr=False)
    _directories: tuple = field(repr=False)
    _locks: tuple = field(repr=False)
    _workspace: Path | None = field(repr=False)
    _include_opencode: bool = field(repr=False)
    _native_database: bool = field(repr=False)

    @property
    def file_count(self):
        return sum(entry.raw is not None for entry in self._entries)

    @property
    def directory_count(self):
        return sum(entry.raw is None for entry in self._entries)


def _selected(entries, include_opencode):
    # Consume the decoder's same bounded shape and path policy. Revalidate
    # structurally without serializing (and making another plaintext copy).
    if (
        type(entries) is not dict
        or len(entries) > payload.MAX_FILES + payload.MAX_DIRECTORIES
    ):
        raise _Refusal("invalid-payload")
    checked = payload._Entries()
    try:
        for key, raw in entries.items():
            if type(key) is not tuple or len(key) != 2:
                raise _Refusal("invalid-payload")
            checked.add(*key, raw)
        checked.finish()
        return tuple(
            _Entry(scope, payload._entry_parts(scope, name), raw)
            for (scope, name), raw in sorted(checked.values.items())
            if scope not in _NATIVE or include_opencode
        )
    except (payload.HostPayloadError, UnicodeError):
        raise _Refusal("invalid-payload") from None


def _roots(workspace):
    try:
        roots = payload.destinations()
    except payload.HostPayloadError:
        raise _Refusal("unsafe-destination") from None
    if any(len(os.fsencode(root)) >= 4096 for root in roots.values()):
        raise _Refusal("unsafe-destination")
    if workspace is not None:
        if workspace.anchor != "/" or ".." in workspace.parts:
            raise _Refusal("unsafe-destination")
        if any(
            workspace.is_relative_to(root) or root.is_relative_to(workspace)
            for root in roots.values()
        ):
            raise _Refusal("workspace-overlap")
    return roots


def _identity(info):
    return info.st_dev, info.st_ino


def _directory_stamp(fd):
    info = os.fstat(fd)
    return (
        *_identity(info),
        payload.mount_id(fd),
        info.st_uid,
        info.st_gid,
        info.st_mode,
    )


def _check_directory(fd, *, owned, private):
    log_config._check_config_directory(fd, leaf=owned)
    if private and os.fstat(fd).st_mode & 0o077:
        raise _Refusal("unsafe-destination")


def _entry_stat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


class _Namespace:
    def __init__(self, root_fd, roots, *, reviewed=None, progress=None):
        self.root_fd = root_fd
        self.roots = roots
        self.progress = progress
        self.reviewing = reviewed is None
        self.stamps = {} if reviewed is None else dict(reviewed)
        self.aliases = {
            stamp[:2]: path for path, stamp in self.stamps.items() if stamp is not None
        }

    def _observe(self, path, stamp):
        if path in self.stamps and stamp != self.stamps[path]:
            raise _Refusal("destination-changed")
        if stamp is not None and self.aliases.setdefault(stamp[:2], path) != path:
            raise _Refusal("unsafe-destination")
        self.stamps[path] = stamp

    def _check(self, fd, scope, path):
        root = self.roots[scope].parts[1:]
        anchor = root[:-2] if scope == payload.VAULT else root[:-1]
        owned = len(path) >= len(root)
        _check_directory(fd, owned=owned, private=owned and scope == payload.VAULT)
        stamp = _directory_stamp(fd)
        if len(path) > len(anchor) and stamp[2] != self.stamps[anchor][2]:
            raise _Refusal("unsafe-destination")
        return stamp

    @staticmethod
    def _writable(parent):
        if not os.access(".", os.W_OK | os.X_OK, dir_fd=parent, effective_ids=True):
            raise _Refusal("unsafe-destination")

    def _create(self, parent, name):
        self.progress.uncertain = True
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            self.progress.uncertain = False
            raise _Refusal("destination-changed") from None
        except OSError:
            self.progress.uncertain = False
            raise
        self.progress.directories += 1
        return os.open(name, _DIR_FLAGS, dir_fd=parent)

    @contextmanager
    def walk(self, scope, parts, *, create=False):
        """Walk from a pinned root; compare known entries and refuse adoption."""
        parent = os.dup(self.root_fd)
        try:
            self._observe((), self._check(parent, scope, ()))
            for depth, name in enumerate(parts, 1):
                path = parts[:depth]
                created = False
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    self._observe(path, None)
                    if not create:
                        if self.reviewing:
                            self._writable(parent)
                        for further in range(depth + 1, len(parts) + 1):
                            self._observe(parts[:further], None)
                        yield None
                        return
                    child = self._create(parent, name)
                    created = True
                try:
                    stamp = self._check(child, scope, path)
                    if created:
                        # Confirm the opened child is still linked here. mkdir
                        # does not return an fd; same-UID races remain possible.
                        current = _entry_stat(parent, name)
                        if current is None or _identity(current) != stamp[:2]:
                            raise _Refusal("destination-changed")
                        del self.stamps[path]
                        self._observe(path, stamp)
                        os.fsync(child)
                        os.fsync(parent)
                        self.progress.uncertain = False
                    else:
                        self._observe(path, stamp)
                except BaseException:
                    os.close(child)
                    raise
                os.close(parent)
                parent = child
            yield parent
        finally:
            os.close(parent)


def _lock_stamp(scope, info):
    if info is None:
        return None
    if scope == payload.OC_BOX_CONFIG:
        log_config._check_lock_file(info)
    elif (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise _Refusal("unsafe-destination")
    return log_config._stamp(info)


@contextmanager
def _pin_lock(scope, parent):
    name = _LOCKS[scope]
    try:
        fd = os.open(name, os.O_PATH | _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        yield None, None
        return
    try:
        if payload.mount_id(fd) != payload.mount_id(parent):
            raise _Refusal("unsafe-destination")
        stamp = _lock_stamp(scope, os.fstat(fd))
        if stamp != _lock_stamp(scope, _entry_stat(parent, name)):
            raise _Refusal("destination-changed")
        yield fd, stamp
    finally:
        os.close(fd)


def _read_lock_stamp(scope, parent):
    with _pin_lock(scope, parent) as (_, stamp):
        return stamp


def _path(namespace, entry):
    return (*namespace.roots[entry.scope].parts[1:], *entry.relative)


def _native_database(entry):
    return entry.scope == payload.OPENCODE_AUTH and entry.relative == _NATIVE_DB


def _check_native_neighbors(directory):
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name not in ("auth.json", "mcp-auth.json"):
                raise _Refusal("occupied")
            with payload._pin(directory, entry.name, payload.mount_id(directory)) as fd:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                ):
                    raise _Refusal("unsafe-destination")


def _inspect(namespace, entries):
    lock_stamps = {}
    for entry in entries:
        parts = _path(namespace, entry)
        with namespace.walk(entry.scope, parts[:-1]) as parent:
            if parent is not None:
                namespace._writable(parent)
                if _entry_stat(parent, parts[-1]) is not None:
                    raise _Refusal("occupied")
                if _native_database(entry):
                    _check_native_neighbors(parent)
            if entry.raw is None:
                namespace._observe(parts, None)
            elif entry.scope in _LOCKS:
                lock_stamps[entry.scope] = (
                    None if parent is None else _read_lock_stamp(entry.scope, parent)
                )
    return lock_stamps


def preflight(
    entries,
    *,
    include_opencode=False,
    workspace_destination=None,
    native_database=False,
):
    """Review current XDG targets without creating directories, locks or files."""
    try:
        if (
            type(include_opencode) is not bool
            or type(native_database) is not bool
            or (native_database and not include_opencode)
        ):
            raise _Refusal("invalid-payload")
        selected = _selected(entries, include_opencode)
        if native_database:
            # Not accepted from the archive's file tree. Only verified native
            # staging supplies these bytes after this destination is reviewed.
            selected = (*selected, _Entry(payload.OPENCODE_AUTH, _NATIVE_DB, b""))
        workspace = (
            None if workspace_destination is None else Path(workspace_destination)
        )
        roots = _roots(workspace)
        with _root_fd() as root:
            namespace = _Namespace(root, roots)
            locks = _inspect(namespace, selected)
        return RestorePlan(
            selected,
            tuple(roots.items()),
            tuple(namespace.stamps.items()),
            tuple(locks.items()),
            workspace,
            include_opencode,
            native_database,
        )
    except _Refusal as exc:
        category = exc.args[0]
    except (OSError, ValueError, TypeError, OverflowError, payload.HostPayloadError):
        category = "unsafe-destination"
    raise HostRestoreError(category) from None


@contextmanager
def _root_fd():
    fd = os.open("/", _DIR_FLAGS)
    try:
        yield fd
    finally:
        os.close(fd)


def _check_lock(scope, parent, fd):
    if payload.mount_id(fd) != payload.mount_id(parent) or _lock_stamp(
        scope, os.fstat(fd)
    ) != _read_lock_stamp(scope, parent):
        raise _Refusal("destination-changed")


@contextmanager
def _lock(namespace, scope, reviewed, progress):
    parts = namespace.roots[scope].parts[1:]
    with namespace.walk(scope, parts, create=True) as parent:
        name = _LOCKS[scope]
        created = reviewed is None
        with _pin_lock(scope, parent) as (pinned, stamp):
            if stamp != reviewed:
                raise _Refusal("destination-changed")
            try:
                if created:
                    progress.uncertain = True
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                        0o600,
                        dir_fd=parent,
                    )
                    progress.locks += 1
                else:
                    # Reopen only the classified regular inode.
                    fd = os.open(
                        f"/proc/self/fd/{pinned}",
                        os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK,
                    )
            except OSError as exc:
                progress.uncertain = False
                if exc.errno in (errno.EEXIST, errno.ENOENT, errno.ELOOP):
                    raise _Refusal("destination-changed") from None
                raise
            try:
                if created:
                    os.fchmod(fd, 0o600)
                    os.fsync(fd)
                    os.fsync(parent)
                elif _lock_stamp(scope, os.fstat(fd)) != reviewed:
                    raise _Refusal("destination-changed")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise _Refusal("locked") from None
                _check_lock(scope, parent, fd)
                progress.uncertain = False
                yield fd
            finally:
                os.close(fd)


def _file_stamp(info, size, *, links):
    if (
        info is None
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_size != size
        or info.st_nlink != links
    ):
        raise _Refusal("destination-changed")
    return _identity(info), info.st_mtime_ns, info.st_ctime_ns


def _publish(namespace, entry, locks, progress):
    parts = _path(namespace, entry)
    raw = entry.raw
    with namespace.walk(entry.scope, parts[:-1], create=True) as parent:
        try:
            fd = os.open(
                ".", os.O_TMPFILE | os.O_WRONLY | os.O_CLOEXEC, 0o600, dir_fd=parent
            )
        except OSError as exc:
            if exc.errno in (
                errno.ENOSYS,
                errno.EINVAL,
                errno.EOPNOTSUPP,
                errno.EISDIR,
                errno.ENOENT,
            ):
                raise _Refusal("atomic-unavailable") from None
            raise
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise _Refusal("io-failure")
                remaining = remaining[written:]
            os.fsync(fd)
            staged = _file_stamp(os.fstat(fd), len(raw), links=0)
            proc_entry = f"/proc/self/fd/{fd}"
            try:
                proc_stamp = _file_stamp(os.stat(proc_entry), len(raw), links=0)
            except (OSError, _Refusal):
                raise _Refusal("atomic-unavailable") from None
            if proc_stamp != staged:
                raise _Refusal("atomic-unavailable")
            with namespace.walk(entry.scope, parts[:-1]) as current:
                if current is None or _directory_stamp(current) != _directory_stamp(
                    parent
                ):
                    raise _Refusal("destination-changed")
                if entry.scope in locks:
                    _check_lock(entry.scope, current, locks[entry.scope])
                if _native_database(entry):
                    _check_native_neighbors(current)
                progress.uncertain = True
                try:
                    # With dst_dir_fd, Python calls linkat with
                    # AT_SYMLINK_FOLLOW; it cannot replace any name occupant.
                    os.link(
                        proc_entry, parts[-1], dst_dir_fd=current, follow_symlinks=True
                    )
                except FileExistsError:
                    progress.uncertain = False
                    raise _Refusal("occupied") from None
                except OSError as exc:
                    progress.uncertain = False
                    if exc.errno in (
                        errno.ENOENT,
                        errno.EXDEV,
                        errno.EOPNOTSUPP,
                        errno.EPERM,
                    ):
                        raise _Refusal("atomic-unavailable") from None
                    raise
                progress.files += 1
                os.fsync(fd)
                os.fsync(current)
                installed = _file_stamp(os.fstat(fd), len(raw), links=1)
                if installed[:2] != staged[:2]:
                    raise _Refusal("destination-changed")
            return installed
        finally:
            os.close(fd)


def _check_published(namespace, entry, staged):
    parts = _path(namespace, entry)
    with namespace.walk(entry.scope, parts[:-1]) as parent:
        if parent is None:
            raise _Refusal("destination-changed")
        try:
            fd = os.open(parts[-1], os.O_PATH | _FILE_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            raise _Refusal("destination-changed") from None
        try:
            if (
                payload.mount_id(fd) != payload.mount_id(parent)
                or _file_stamp(os.fstat(fd), len(entry.raw), links=1) != staged
                or _file_stamp(_entry_stat(parent, parts[-1]), len(entry.raw), links=1)
                != staged
            ):
                raise _Refusal("destination-changed")
        finally:
            os.close(fd)


def apply(snapshot, *, include_opencode=False, native_database=None):
    """Recheck and activate after consent; return the installed file count.

    An earlier preflight plan additionally binds activation to the reviewed
    namespace. Otherwise take a new complete preflight now.
    """
    if type(include_opencode) is not bool:
        raise HostRestoreError("invalid-payload")
    if isinstance(snapshot, RestorePlan):
        plan = snapshot
        if plan._include_opencode != include_opencode:
            raise HostRestoreError("invalid-payload")
    else:
        plan = preflight(snapshot, include_opencode=include_opencode)
    if plan._native_database:
        if (
            type(native_database) is not bytes
            or not 0 < len(native_database) <= _NATIVE_DB_LIMIT
            or not native_database.startswith(b"SQLite format 3\0")
        ):
            raise HostRestoreError("invalid-payload")
    elif native_database is not None:
        raise HostRestoreError("invalid-payload")
    progress = _Progress()
    try:
        roots = _roots(plan._workspace)
        if tuple(roots.items()) != plan._roots:
            raise _Refusal("destination-changed")
        if not plan._entries:
            return 0
        if not hasattr(os, "O_TMPFILE") and plan.file_count:
            raise _Refusal("atomic-unavailable")
        with _root_fd() as root, ExitStack() as stack:
            namespace = _Namespace(
                root, roots, reviewed=plan._directories, progress=progress
            )
            reviewed_locks = dict(plan._locks)
            if _inspect(namespace, plan._entries) != reviewed_locks:
                raise _Refusal("destination-changed")
            locks = {
                scope: stack.enter_context(_lock(namespace, scope, stamp, progress))
                for scope, stamp in reviewed_locks.items()
            }
            published = []
            for entry in plan._entries:
                if entry.raw is None:
                    with namespace.walk(
                        entry.scope, _path(namespace, entry), create=True
                    ):
                        pass
                else:
                    if _native_database(entry):
                        entry = _Entry(entry.scope, entry.relative, native_database)
                    staged = _publish(namespace, entry, locks, progress)
                    _check_published(namespace, entry, staged)
                    published.append((entry, staged))
                    progress.uncertain = False
            # Check the whole reviewed result again, not only the last file.
            progress.uncertain = True
            if _roots(plan._workspace) != roots:
                raise _Refusal("destination-changed")
            for entry in plan._entries:
                if entry.raw is None:
                    with namespace.walk(entry.scope, _path(namespace, entry)):
                        pass
            for entry, staged in published:
                _check_published(namespace, entry, staged)
            progress.uncertain = False
        return progress.files
    except _Refusal as exc:
        category = exc.args[0]
    except KeyboardInterrupt:
        category = "interrupted"
    except (ValueError, payload.HostPayloadError):
        category = "unsafe-destination"
    except OSError:
        category = "io-failure"
    if category == "destination-changed" and (
        progress.files or progress.directories or progress.locks
    ):
        progress.uncertain = True
    raise HostRestoreError(category, progress.result()) from None
