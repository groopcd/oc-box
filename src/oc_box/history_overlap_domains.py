"""Metadata-only physical separation checks for ordinary quiet-source read-ahead.

These are not overlap admission or native path discovery. A caller must first
bind the original local Podman resource and its stock native process, prove the
complete write footprint of both exact commands, and fence those commands'
selectors, config, executable, and cwd. In particular a ``debug paths`` reply
does not prove a footprint, and running that command can write. No native or
engine commands run here. Inability to acquire a proof is a serial opt-out
*before* speculative work, not permission to replay or ignore an in-flight fault.
Point-in-time checks neither confine a future pathname open nor exclude
non-cooperating same-user changes between checks.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
import re
import select
import stat
import threading
import unicodedata

if __package__:
    from . import file_preview_io as linux
else:
    import file_preview_io as linux


_EXACT = frozenset({"2.4.0", "2.4.1"})
# One direct local host mount and one fstat device for the comparison. A
# different mount (even with equal st_dev), an overlay, or a subvolume with a
# different st_dev is not a proof of disjoint ancestry.
_DIRECT_FS = frozenset({b"btrfs", b"ext4", b"xfs"})
_SCOPES = frozenset(
    {
        "data",
        "migration",
        "blobs",
        "state",
        "registration",
        "log",
        "cache",
        "config",
        "bin",
        "repos",
        "home-fallback",
        "runtime",
        "temp",
        "cwd",
    }
)
_PATH_LIMIT = 4095
_PATH_PARTS = 32
_TREE_DEPTH = 24
_TREE_ENTRIES = 4096
_FD_ENTRIES = 512
_DIRECTORY = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
_METADATA = os.O_PATH | os.O_CLOEXEC
_SIDECARS = (b"-wal", b"-shm", b"-journal")
_SOURCE_DB = b"/.local/share/opencode/oc-box-v2.db"


class DomainRefusal(ValueError):
    """Fixed refusal; never include host paths or command data in a message."""


def _refuse(reason):
    raise DomainRefusal(reason)


@contextmanager
def _fixed_errors():
    try:
        yield
    except DomainRefusal:
        raise
    except (OSError, ValueError):
        raise DomainRefusal("writable-domain-unverified") from None


def _keep(stack, fd):
    stack.callback(os.close, fd)
    return fd


def _path(value):
    if type(value) is not str or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value
    ):
        _refuse("writable-path-policy")
    raw = os.fsencode(value)
    parts = raw[1:].split(b"/")
    if (
        not raw.startswith(b"/")
        or len(raw) > _PATH_LIMIT
        or len(parts) > _PATH_PARTS
        or any(p in (b"", b".", b"..") for p in parts)
        or any(_within(raw, block) for block in (b"/proc", b"/sys", b"/dev", b"/etc"))
    ):
        _refuse("writable-path-policy")
    return raw


def _within(path, root):
    return path == root or path.startswith(root.rstrip(b"/") + b"/")


def _parent(path):
    return path.rsplit(b"/", 1)[0] or b"/"


def _process_start(proc, expected_pid):
    data = linux._proc_read(proc, b"stat", 8192)
    # comm can contain spaces and parentheses. The trailing fields cannot.
    prefix, end, tail = data.rpartition(b") ")
    fields = tail.split()
    if (
        not end
        or not prefix.startswith(str(expected_pid).encode() + b" (")
        or len(fields) <= 19
        or fields[0] in (b"Z", b"X", b"x")
        or not re.fullmatch(rb"[0-9]{1,20}", fields[19])
        or int(fields[19]) == 0
    ):
        _refuse("process-binding-unverified")
    return int(fields[19])


def _bare_stamp(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _directory(info, uid, *, parent=False):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_nlink == 0
        or info.st_uid not in ((0, uid) if parent else (uid,))
        or (
            info.st_mode & 0o022
            and not (parent and info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
        )
    ):
        _refuse("writable-path-policy")


def _file(info, uid):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != uid
        or info.st_mode & 0o022
    ):
        _refuse("writable-path-policy")


@dataclass(frozen=True)
class _Object:
    device: int
    inode: int
    mount: int
    owner: int
    group: int
    mode: int

    @property
    def backing(self):
        # A mount ID is namespace-specific. Compare it for stability within a
        # view, NEVER as a test of identity between a container and the host.
        return self.device, self.inode


class _Namespace:
    def __init__(self, observer, target, pidfd, ns, root, pid, start):
        self.observer = observer
        self.target = target
        self.pidfd = pidfd
        self.ns = ns
        self.root = root
        self.pid = pid
        self.start = start
        self.closed = False
        self.stock_executable = None
        self.root_object = self.object(root)
        self.mountinfo = self.get_mountinfo()
        self.mounts = linux._mounts(self.mountinfo)
        root_mount = self.exact_mount(b"/")
        if self.root_object.mount != root_mount[0]:
            _refuse("mount-binding-unverified")

    def object(self, fd):
        info = os.fstat(fd)
        return _Object(
            info.st_dev,
            info.st_ino,
            linux._mount_id(self.observer, fd),
            info.st_uid,
            info.st_gid,
            info.st_mode,
        )

    def get_mountinfo(self):
        return linux._proc_read(self.target, b"mountinfo", linux.MAX_MOUNTINFO)

    def exact_mount(self, path):
        return linux._exact_mount(self.mounts, path)

    def recheck(self):
        with _fixed_errors(), ExitStack() as stack:
            if self.closed or threading.get_native_id() != os.getpid():
                _refuse("process-binding-changed")
            poll = select.poll()
            poll.register(self.pidfd, select.POLLIN)
            if poll.poll(0) or _process_start(self.target, self.pid) != self.start:
                _refuse("process-binding-changed")
            ns = _keep(
                stack,
                os.open(b"ns/mnt", os.O_RDONLY | os.O_CLOEXEC, dir_fd=self.target),
            )
            root = _keep(stack, os.open(b"root", _DIRECTORY, dir_fd=self.target))
            if (
                _bare_stamp(ns) != _bare_stamp(self.ns)
                or self.object(self.root) != self.root_object
                or self.object(root) != self.root_object
                or self.get_mountinfo() != self.mountinfo
                or poll.poll(0)
            ):
                _refuse("mount-binding-changed")
            executable = getattr(self, "stock_executable", None)
            if executable is not None:
                # This is the one independently pinned native process,
                # not a pathname taken from a native/API reply.
                info = os.stat(b"exe", dir_fd=self.target)
                if (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ) != executable or poll.poll(0):
                    _refuse("process-binding-changed")

    def walk(self, stack, path, uid, *, file=False):
        """Resolve every component with no links and only visible mount edges."""
        current = self.root
        parent = self.root_object
        _directory(os.fstat(current), uid, parent=True)
        scope = b""
        parents = [parent]
        parts = path[1:].split(b"/")
        with ExitStack() as parents_stack:
            for i, part in enumerate(parts):
                scope += b"/" + part
                leaf = i == len(parts) - 1
                mount = self.mounts.get(scope)
                if mount and (len(mount) != 1 or mount[0][1] != parent.mount):
                    _refuse("mount-binding-unverified")
                fd = _keep(
                    stack if leaf else parents_stack,
                    linux._openat2(
                        current,
                        part,
                        _METADATA if leaf and file else _DIRECTORY,
                        cross=bool(mount),
                    ),
                )
                obj = self.object(fd)
                if obj.mount != (mount[0][0] if mount else parent.mount):
                    _refuse("mount-binding-changed")
                if leaf and file:
                    _file(os.fstat(fd), uid)
                else:
                    _directory(os.fstat(fd), uid, parent=not leaf)
                parents.append(obj)
                current, parent = fd, obj
            return current, tuple(parents)

    def no_submount(self, path):
        if any(other != path and _within(other, path) for other in self.mounts):
            _refuse("writable-tree-mount")

    def require_cwd(self, path):
        """Match a live process's kernel cwd, not the environment's PWD."""
        with _fixed_errors(), ExitStack() as stack:
            self.recheck()
            fd, _ = self.walk(stack, _path(path), os.geteuid())
            actual = _keep(stack, os.open(b"cwd", _DIRECTORY, dir_fd=self.target))
            if self.object(fd) != self.object(actual):
                _refuse("writable-cwd-unverified")
            self.recheck()


@contextmanager
def _pin_namespace(pid, expected_start):
    with ExitStack() as stack:
        with _fixed_errors():
            pinned = _acquire_namespace(stack, pid, expected_start)
        try:
            yield pinned
        finally:
            pinned.closed = True


def _acquire_namespace(stack, pid, expected_start):
    if (
        os.uname().sysname != "Linux"
        or os.uname().machine not in linux.ARCHITECTURES
        or not hasattr(os, "pidfd_open")
        # Child creation must inherit the same namespace as the one sampled
        # here. /proc/<pid> describes the leader, not a worker's unshared view.
        or threading.get_native_id() != os.getpid()
        or type(pid) is not int
        or not 1 <= pid < 2**31
        or (expected_start is None and pid != os.getpid())
    ):
        _refuse("process-binding-unverified")
    proc = _keep(stack, os.open("/proc", _DIRECTORY | os.O_NOFOLLOW))
    observer_pid = str(os.getpid()).encode()
    if not linux._procfs(proc) or os.readlink(b"self", dir_fd=proc) != observer_pid:
        _refuse("process-binding-unverified")
    observer = _keep(stack, linux._openat2(proc, observer_pid, _DIRECTORY))
    pidfd = _keep(stack, os.pidfd_open(pid))
    target = _keep(stack, linux._openat2(proc, str(pid).encode(), _DIRECTORY))
    start = _process_start(target, pid)
    if expected_start is not None and start != expected_start:
        _refuse("process-binding-changed")
    # These are the only intentional procfs magic-link follows. Resolve
    # relative to the already pinned exact process, never a native selector.
    ns = _keep(stack, os.open(b"ns/mnt", os.O_RDONLY | os.O_CLOEXEC, dir_fd=target))
    root = _keep(stack, os.open(b"root", _DIRECTORY, dir_fd=target))
    pinned = _Namespace(observer, target, pidfd, ns, root, pid, start)
    pinned.recheck()
    return pinned


def pin_process_namespace(pid, *, start_ticks):
    """Pin an externally identified host-visible PID and verified start tick.

    Passing a PID is not evidence that it is the original quiet native process.
    A denied /proc root/ns lookup, missing pidfd, or mount drift has no fallback.
    """
    if type(start_ticks) is not int or not 1 <= start_ticks < 2**64:
        _refuse("process-binding-unverified")
    return _pin_namespace(pid, start_ticks)


def observe_process_start(pid):
    """Sample an externally selected PID; caller still verifies its resource."""
    if type(pid) is not int or not 1 <= pid < 2**31:
        _refuse("process-binding-unverified")
    with _fixed_errors(), ExitStack() as stack:
        proc = _keep(stack, os.open("/proc", _DIRECTORY | os.O_NOFOLLOW))
        if (
            not linux._procfs(proc)
            or os.readlink(b"self", dir_fd=proc) != str(os.getpid()).encode()
        ):
            _refuse("process-binding-unverified")
        target = _keep(stack, linux._openat2(proc, str(pid).encode(), _DIRECTORY))
        return _process_start(target, pid)


def pin_current_namespace():
    """Pin the launcher's view, used by the unmodified host command."""
    return _pin_namespace(os.getpid(), None)


@dataclass(frozen=True)
class _PathPin:
    space: _Namespace
    path: bytes
    fd: int
    chain: tuple[_Object, ...]
    file: bool

    @property
    def obj(self):
        return self.chain[-1]

    def recheck(self):
        with ExitStack() as stack:
            _fd, current = self.space.walk(
                stack, self.path, os.geteuid(), file=self.file
            )
            if current != self.chain or self.space.object(self.fd) != self.obj:
                _refuse("writable-path-changed")
            if self.file:
                _file(os.fstat(self.fd), os.geteuid())


def _pin_path(stack, space, path, *, file=False):
    fd, chain = space.walk(stack, path, os.geteuid(), file=file)
    return _PathPin(space, path, fd, chain, file)


def _mount_fs(row):
    fields = row[2].split(b" ")
    sep = fields.index(b"-", 6)
    return fields[2], fields[sep + 1]


def _require_direct_mount(remote, remote_mount, local, host_mount):
    def writable(row):
        fields = row[2].split(b" ")
        return all(b"rw" in field.split(b",") for field in (fields[5], fields[-1]))

    # mountinfo's device describes the superblock. It may differ from
    # fstat's st_dev (e.g. Btrfs). Keep these two checks independent.
    if (
        remote.obj.mount != remote_mount[0]
        or remote.chain[-2].mount != remote_mount[1]
        or remote.obj.mount == remote.chain[-2].mount
        or local.obj.mount != host_mount[0]
        or remote.obj.backing != local.obj.backing
        or _mount_fs(remote_mount) != _mount_fs(host_mount)
        or _mount_fs(host_mount)[1] not in _DIRECT_FS
        or not writable(remote_mount)
        or not writable(host_mount)
    ):
        _refuse("source-backing-unverified")


class _Backing:
    def __init__(self, source, host):
        self.source = source
        self.host = host
        self.closed = False

    def recheck(self):
        with _fixed_errors():
            if self.closed:
                _refuse("source-backing-unverified")
            self.source.space.recheck()
            self.host.space.recheck()
            self.source.recheck()
            self.host.recheck()
            if self.source.obj.backing != self.host.obj.backing:
                _refuse("source-backing-unverified")

    def scratch_binding(self, release):
        """Report only the verified original local-volume backing to the lease."""
        if type(release) is not str or release not in ("2.4.0", "2.4.1"):
            _refuse("unsupported-version")
        self.recheck()
        return {
            "scope": "exact-ordinary-quiet-source-v1",
            "version": release,
            "home": os.fsdecode(self.source.path),
            "device": self.source.obj.device,
            "inode": self.source.obj.inode,
            "mount_id": self.source.obj.mount,
            "namespace": _bare_stamp(self.source.space.ns),
        }


@contextmanager
def pin_mount_backing(source, mount_path, host, backing_path):
    """Pair an exact source mount with its independently verified host backing.

    First use the existing lifecycle/resource checks to bind a local engine,
    immutable running container and named, optionless local volume, including
    the actual host mountpoint. Inputs must be those checked paths, not guessed
    paths or native declarations. Recheck that resource contract separately.
    This function establishes physical correspondence, not Podman ownership.
    """
    with ExitStack() as stack:
        with _fixed_errors():
            result = _acquire_backing(stack, source, mount_path, host, backing_path)
        try:
            yield result
        finally:
            result.closed = True


def _acquire_backing(stack, source, mount_path, host, backing_path):
    if type(source) is not _Namespace or type(host) is not _Namespace:
        _refuse("source-backing-unverified")
    source.recheck()
    host.recheck()
    if host.pid != os.getpid() or _bare_stamp(source.ns) == _bare_stamp(host.ns):
        _refuse("source-backing-unverified")
    remote = _pin_path(stack, source, _path(mount_path))
    local = _pin_path(stack, host, _path(backing_path))
    remote_mount = source.exact_mount(remote.path)
    host_mount = next(
        (
            row
            for rows in host.mounts.values()
            for row in rows
            if row[0] == local.obj.mount
        ),
        None,
    )
    if host_mount is None:
        _refuse("source-backing-unverified")
    _require_direct_mount(remote, remote_mount, local, host_mount)
    result = _Backing(remote, local)
    result.recheck()
    return result


@dataclass(frozen=True)
class WritablePaths:
    """Reviewed effective roots for one exact native command environment.

    A root is an existing scoped directory, not an XDG/HOME wildcard. Each
    scope is mandatory; dirs may repeat where independently audited operations
    use the same directory. Absent or unbounded scopes are not yet supported.
    ``homes`` identifies the actual HOME and native test-home/OS-home
    selectors, not authorized writable roots. Cover their applicable
    fallbacks *explicitly* with the home-fallback scope.
    """

    database: str
    roots: tuple[tuple[str, str], ...]
    homes: tuple[str, ...]


def _layout(layout):
    if (
        type(layout) is not WritablePaths
        or type(layout.roots) is not tuple
        or not len(_SCOPES) <= len(layout.roots) <= 2 * len(_SCOPES)
        or type(layout.homes) is not tuple
        or not 1 <= len(layout.homes) <= 3
    ):
        _refuse("writable-roots-incomplete")
    homes = tuple(map(_path, layout.homes))
    scopes = {}
    seen = set()
    for pair in layout.roots:
        if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str:
            _refuse("writable-roots-incomplete")
        scope, location = pair[0], _path(pair[1])
        if (
            scope not in _SCOPES
            or (scope, location) in seen
            or any(_within(home, location) for home in homes)
            or (
                scope == "home-fallback"
                and not any(_within(location, h) for h in homes)
            )
        ):
            _refuse("writable-path-policy")
        seen.add((scope, location))
        scopes.setdefault(scope, []).append(location)
    db = _path(layout.database)
    if (
        scopes.keys() != _SCOPES
        or len(scopes["cwd"]) != 1
        or not any(_within(_parent(db), root) for root in scopes["data"])
    ):
        _refuse("writable-roots-incomplete")
    return db, {location for _scope, location in seen}, scopes["cwd"][0]


def _on_disk(stack, namespace, location, *, mappings, file=False):
    original = _pin_path(stack, namespace, location, file=file)
    if not mappings:
        return original, original
    candidates = [
        mapping for mapping in mappings if _within(location, mapping.source.path)
    ]
    if len(candidates) != 1:
        _refuse("source-backing-unverified")
    mapping = candidates[0]
    if location == mapping.source.path and (file or mapping is mappings[0]):
        # An independently matched scratch mount may itself be the audited
        # temp or cwd root. The named home volume is never a wildcard domain.
        _refuse("writable-path-policy")
    path = mapping.host.path + location[len(mapping.source.path) :]
    physical = _pin_path(stack, mapping.host.space, _path(os.fsdecode(path)), file=file)
    if (
        original.obj.mount != mapping.source.obj.mount
        or physical.obj.mount != mapping.host.obj.mount
        or original.obj.backing != physical.obj.backing
    ):
        _refuse("source-backing-unverified")
    return original, physical


def _directory_times(fd):
    obj = os.fstat(fd)
    return obj.st_mtime_ns, obj.st_ctime_ns


def _tree(pin, budget):
    """Check all existing edges in a bounded scoped tree; open no file data."""
    pin.space.no_submount(pin.path)
    uid = os.geteuid()
    directories = set()

    def scan(fd, depth):
        if depth > _TREE_DEPTH:
            _refuse("writable-tree-limit")
        before = _directory_times(fd)
        with ExitStack() as stack:
            readable = _keep(
                stack, linux._openat2(fd, b".", os.O_RDONLY | os.O_DIRECTORY)
            )
            with os.scandir(readable) as listing:
                for entry in listing:
                    budget[0] -= 1
                    if budget[0] < 0:
                        _refuse("writable-tree-limit")
                    with ExitStack() as child_stack:
                        child = _keep(
                            child_stack,
                            linux._openat2(fd, os.fsencode(entry.name), _METADATA),
                        )
                        info = os.fstat(child)
                        if (
                            info.st_dev != pin.obj.device
                            or linux._mount_id(pin.space.observer, child)
                            != pin.obj.mount
                        ):
                            _refuse("writable-tree-mount")
                        if stat.S_ISDIR(info.st_mode):
                            _directory(info, uid)
                            key = info.st_dev, info.st_ino
                            if key in directories:
                                _refuse("writable-tree-alias")
                            directories.add(key)
                            scan(child, depth + 1)
                        else:
                            _file(info, uid)
        if before != _directory_times(fd):
            _refuse("writable-tree-changed")

    directories.add(pin.obj.backing)
    scan(pin.fd, 0)


def _sidecars(pin):
    """Sample exact adjacent SQLite paths without requiring WAL/SHM persistence."""
    with ExitStack() as stack:
        parent, chain = pin.space.walk(stack, _parent(pin.path), os.geteuid())
        if chain != pin.chain[:-1] or chain[-1].mount != pin.obj.mount:
            _refuse("writable-path-changed")
        name = pin.path.rsplit(b"/", 1)[1]
        result = []
        for suffix in _SIDECARS:
            leaf = name + suffix
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                result.append(None)
                continue
            fd = _keep(stack, linux._openat2(parent, leaf, _METADATA))
            _file(os.fstat(fd), os.geteuid())
            obj = pin.space.object(fd)
            if (obj.device, obj.mount) != (pin.obj.device, pin.obj.mount):
                _refuse("writable-tree-mount")
            result.append(obj.backing)
        return tuple(result)


def _require_open_database(pin):
    """Observe the selected inode among the exact quiet process's open FDs."""
    namespace = pin.space
    with ExitStack() as stack:
        fds = _keep(
            stack,
            linux._openat2(namespace.target, b"fd", os.O_RDONLY | os.O_DIRECTORY),
        )
        with os.scandir(fds) as listing:
            for index, entry in enumerate(listing):
                if (
                    index == _FD_ENTRIES
                    or not entry.name.isascii()
                    or not entry.name.isdecimal()
                ):
                    break
                try:
                    info = os.stat(entry.name, dir_fd=fds)
                    if (info.st_dev, info.st_ino) != pin.obj.backing:
                        continue
                    # Intentional follow of a numeric FD in pinned procfs.
                    # O_PATH carries no database read/write capability.
                    current = _keep(stack, os.open(entry.name, _METADATA, dir_fd=fds))
                except FileNotFoundError:
                    continue
                if namespace.object(current) == pin.obj:
                    _file(os.fstat(current), os.geteuid())
                    return
    _refuse("source-database-not-open")


class _Domains:
    def __init__(self, volume, mappings, source, destination, cwd):
        self.volume = volume
        self.mappings = mappings
        self.source = source
        self.destination = destination
        self.cwd = cwd
        self.closed = False
        self.failed = False

    def recheck(self):
        """Point-in-time recheck, on the coordinator before work admission."""
        if self.closed or self.failed:
            _refuse("writable-domain-ended")
        try:
            with _fixed_errors():
                self._check()
        except BaseException:
            # A changed/unknown view never becomes fresh evidence within this
            # pass. Do not retry by choosing a new baseline after a fault.
            self.failed = True
            raise

    def require_destination(self, layout):
        """Bind the writer's configured paths to this pass's pinned target view."""
        try:
            db, roots, _cwd = _layout(layout)
            expected = tuple((db, *sorted(roots)))
            observed = tuple(path.path for path, _physical in self.destination)
            if expected != observed:
                _refuse("writable-selector-unverified")
            self.recheck()
        except BaseException:
            self.failed = True
            raise

    def _check(self):
        for mapping in self.mappings:
            mapping.recheck()
        self.volume.source.space.require_cwd(os.fsdecode(self.cwd))
        entries = self.source + self.destination
        for remote, physical in entries:
            remote.recheck()
            if physical is not remote:
                physical.recheck()
                if remote.obj.backing != physical.obj.backing:
                    _refuse("source-backing-unverified")
            if (physical.obj.device, physical.obj.mount) != (
                self.volume.host.obj.device,
                self.volume.host.obj.mount,
            ):
                _refuse("common-host-mount-unverified")
        left, right = (
            tuple(x[1] for x in self.source),
            tuple(x[1] for x in self.destination),
        )
        for a in left:
            for b in right:
                if a.obj.backing in {
                    obj.backing for obj in b.chain
                } or b.obj.backing in {obj.backing for obj in a.chain}:
                    _refuse("overlapping-writable-domains")
        # Compare sidecars across the two source views, not against a pass-old
        # snapshot. Native SQLite may cycle a legitimate WAL or SHM between
        # operations. A changed observation during a single check refuses.
        source_db, source_file = self.source[0]
        host_db = self.destination[0][0]
        _require_open_database(source_db)
        source_before = _sidecars(source_db)
        host_before = _sidecars(host_db)
        budget = [_TREE_ENTRIES]
        for remote, physical in entries:
            if not remote.file:
                remote.space.no_submount(remote.path)
                # Only scan the outermost verified root for each role/view.
                if not any(
                    physical.space is other.space
                    and physical.path != other.path
                    and _within(physical.path, other.path)
                    for other in (*left[1:], *right[1:])
                ):
                    _tree(physical, budget)
        if (
            _sidecars(source_db) != source_before
            or _sidecars(source_file) != source_before
            or _sidecars(host_db) != host_before
        ):
            _refuse("writable-sidecars-changed")
        for remote, physical in entries:
            remote.recheck()
            if physical is not remote:
                physical.recheck()
        for mapping in self.mappings:
            mapping.recheck()
        self.volume.source.space.require_cwd(os.fsdecode(self.cwd))
        _require_open_database(source_db)
        self.volume.source.space.recheck()


@contextmanager
def pin_writable_domains(
    *, release, volume, additional_source_backings, source, destination, host_cwd
):
    """Prove separation of supplied scoped roots, not how native selects them.

    Caller prerequisites: exact stock matching 2.4.0/2.4.1; ordinary verified
    original stopped-origin quiet source (not a Reader/live owner); immutable
    resource and effective command/selector bindings; full footprint review.
    Each extra source domain must have an independently matched direct mount
    backing. The ordinary source's overlay scratch does not qualify as such
    evidence. The destination DB must be the existing, unchanged actual store;
    no selector, native config, or filesystem state is changed here.
    """
    with ExitStack() as stack:
        with _fixed_errors():
            domains = _acquire_domains(
                stack,
                release,
                volume,
                additional_source_backings,
                source,
                destination,
                host_cwd,
            )
        try:
            yield domains
        finally:
            domains.closed = True


def verify_prelease_separation(volume, destination):
    """Refuse aliased backing before the quiet native source can open its DB."""
    if type(volume) is not _Backing:
        _refuse("source-backing-unverified")
    db, roots, _cwd = _layout(destination)
    with _fixed_errors(), ExitStack() as stack:
        volume.recheck()
        remote, source_db = _on_disk(
            stack,
            volume.source.space,
            volume.source.path + _SOURCE_DB,
            mappings=(volume,),
            file=True,
        )
        base = volume.host
        pinned = tuple(
            _pin_path(stack, base.space, location, file=i == 0)
            for i, location in enumerate((db, *sorted(roots)))
        )
        for path in pinned:
            if (
                (path.obj.device, path.obj.mount) != (base.obj.device, base.obj.mount)
                or base.obj.backing in {item.backing for item in path.chain}
                or path.obj.backing in {item.backing for item in base.chain}
                or source_db.obj.backing == path.obj.backing
            ):
                _refuse("overlapping-writable-domains")
            if not path.file:
                path.space.no_submount(path.path)
            path.recheck()
        remote.recheck()
        source_db.recheck()
        volume.recheck()


def _acquire_domains(stack, release, volume, extra, source, destination, host_cwd):
    if (
        type(release) is not str
        or release not in _EXACT
        or type(volume) is not _Backing
        or type(extra) is not tuple
        or len(extra) > 3
        or any(type(x) is not _Backing for x in extra)
    ):
        _refuse("writable-domain-profile")
    src_db, src_roots, src_cwd = _layout(source)
    dst_db, dst_roots, dst_cwd = _layout(destination)
    if dst_cwd != _path(host_cwd) or src_db != volume.source.path + _SOURCE_DB:
        _refuse("writable-selector-unverified")
    mappings = (volume, *extra)
    for mapping in mappings:
        if (
            mapping.source.space is not volume.source.space
            or mapping.host.space is not volume.host.space
        ):
            _refuse("source-backing-unverified")
        mapping.recheck()
    left, right = tuple(
        tuple(
            _on_disk(stack, namespace, location, mappings=role_mappings, file=i == 0)
            for i, location in enumerate((db, *sorted(roots)))
        )
        for namespace, db, roots, role_mappings in (
            (volume.source.space, src_db, src_roots, mappings),
            (volume.host.space, dst_db, dst_roots, ()),
        )
    )
    domains = _Domains(volume, mappings, left, right, src_cwd)
    domains.recheck()
    return domains
