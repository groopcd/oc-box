"""Opt-in scratch for the exact ordinary quiet source, not a Reader or owner.

Run inside the already verified original container. This stdlib-only helper
can also be loaded as trusted source in its isolated Python process. The
coordinator must hold the original lifecycle admission and independently
verify that the in-container mount is the owned optionless local home volume;
the record below binds that evidence to live kernel objects. This helper
does not inspect Podman, discover native paths, start work, or prove that
native uses every selector. There is no default or weaker fallback.
"""

from contextlib import ExitStack, contextmanager
import ctypes
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time


SCOPE = "exact-ordinary-quiet-source-v1"
_EXACT = ("2.4.0", "2.4.1")
_ARCHITECTURES = {
    "x86_64",
    "aarch64",
    "riscv64",
    "i386",
    "i686",
    "armv7l",
    "ppc64le",
    "s390x",
}
_DIRECT = {b"btrfs", b"ext4", b"xfs"}
_PATH = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", re.ASCII)
_DIR = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
_META = os.O_PATH | os.O_CLOEXEC
_PROC_SUPER_MAGIC = 0x9FA0
_MOUNTINFO_BYTES = 4 * 1024 * 1024
_MAX_DEPTH = 24
_MAX_ENTRIES = 4096
_CLEANUP_SECONDS = 5
_DIRECTORIES = (
    "home",
    "config",
    "state",
    "cache",
    "runtime",
    "version-data",
    "tmp",
    "work",
)


class ScratchRefusal(ValueError):
    """Fixed refusal; do not report paths, native content or OS errors."""


class ScratchCleanupUncertain(ScratchRefusal):
    """Some cleanup cannot be verified; do not retry or fall back."""

    history_source_cleanup_failed = True

    def __init__(self):
        super().__init__("cleanup-failed")


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def _open(parent, path, flags, *, cross=False):
    # BENEATH | NO_SYMLINKS | NO_MAGICLINKS, and NO_XDEV unless this is the
    # exact, separately verified final volume (or initial procfs) edge.
    how = _OpenHow(flags | os.O_CLOEXEC, 0, 0x0E | (0 if cross else 0x01))
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(
        ctypes.c_long(437),
        ctypes.c_int(parent),
        ctypes.c_char_p(path),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if fd < 0:
        raise ScratchRefusal("scratch-path-unverified")
    return fd


def _keep(stack, fd):
    stack.callback(os.close, fd)
    return fd


def _pair(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _read(proc, path, cap):
    fd = _open(proc, path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ScratchRefusal("scratch-mount-unverified")
    return data


def _mount_id(proc, fd):
    records = re.findall(
        rb"^mnt_id:\s*([0-9]{1,20})$",
        _read(proc, f"fdinfo/{fd}".encode(), 4096),
        re.M,
    )
    if len(records) != 1 or int(records[0]) == 0:
        raise ScratchRefusal("scratch-mount-unverified")
    return int(records[0])


def _mount_rows(raw):
    if not raw.endswith(b"\n"):
        raise ScratchRefusal("scratch-mount-unverified")
    rows = {}
    seen = set()
    escapes = {b"040": b" ", b"011": b"\t", b"012": b"\n", b"134": b"\\"}
    for line in raw[:-1].split(b"\n"):
        parts = line.split(b" ")
        if (
            len(parts) < 10
            or not re.fullmatch(rb"[0-9]{1,20}", parts[0])
            or not re.fullmatch(rb"[0-9]{1,20}", parts[1])
        ):
            raise ScratchRefusal("scratch-mount-unverified")
        divider = parts.index(b"-", 6)
        if len(parts) != divider + 4:
            raise ScratchRefusal("scratch-mount-unverified")
        mount, parent = int(parts[0]), int(parts[1])
        target = re.sub(rb"\\(040|011|012|134)", lambda m: escapes[m[1]], parts[4])
        if mount in seen or not target.startswith(b"/"):
            raise ScratchRefusal("scratch-mount-unverified")
        seen.add(mount)
        writable = b"rw" in parts[5].split(b",") and b"rw" in parts[-1].split(b",")
        rows.setdefault(target, []).append(
            (mount, parent, parts[divider + 1], writable)
        )
    return rows


def _verified_mount(rows, home, mount, parent):
    candidates = rows.get(home, ())
    if (
        len(candidates) != 1
        or candidates[0][:2] != (mount, parent)
        or mount == parent
        or candidates[0][2] not in _DIRECT
        or candidates[0][3] is not True
    ):
        raise ScratchRefusal("scratch-volume-mount-unverified")


def _binding(record):
    names = {"scope", "version", "home", "device", "inode", "mount_id", "namespace"}
    if (
        type(record) is not dict
        or record.keys() != names
        or record["scope"] != SCOPE
        or type(record["version"]) is not str
        or record["version"] not in _EXACT
        or type(record["home"]) is not str
    ):
        raise ScratchRefusal("scratch-profile-unverified")
    home = record["home"]
    parts = home.split("/")[1:]
    ns = record["namespace"]
    if (
        len(home) > 1024
        or not _PATH.fullmatch(home)
        or not 1 <= len(parts) <= 16
        or any(p in (".", "..") for p in parts)
        or parts[0] in ("dev", "proc", "sys", "run", "etc", "opt")
        or home == "/tmp"
        or type(ns) not in (tuple, list)
        or len(ns) != 2
        or not all(
            type(value) is int and 0 < value < 2**64
            for value in (record["device"], record["inode"], record["mount_id"], *ns)
        )
    ):
        raise ScratchRefusal("scratch-volume-binding-unverified")
    return (
        home.encode(),
        (record["device"], record["inode"]),
        record["mount_id"],
        tuple(ns),
    )


def _stamp(proc, fd, *, directory, private=False):
    info = os.fstat(fd)
    if (
        info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or (
            not (stat.S_ISDIR(info.st_mode) and info.st_nlink)
            if directory
            else not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        )
        or (private and stat.S_IMODE(info.st_mode) != 0o700)
    ):
        raise ScratchRefusal("scratch-object-unverified")
    # Do not freeze timestamps or directory link counts: native setup and
    # our verified cleanup legitimately change them.
    return (
        info.st_dev,
        info.st_ino,
        _mount_id(proc, fd),
        info.st_uid,
        info.st_gid,
        info.st_mode,
    )


class _Volume:
    def __init__(self, stack, binding):
        machine = os.uname()
        if (
            machine.sysname != "Linux"
            or machine.machine not in _ARCHITECTURES
            or threading.get_native_id() != os.getpid()
        ):
            raise ScratchRefusal("scratch-profile-unverified")
        self.home, self.backing, self.mount, self.namespace = binding
        self.root = _keep(stack, os.open("/", _DIR | os.O_NOFOLLOW))
        proc = _keep(stack, _open(self.root, b"proc", _DIR, cross=True))
        fs = (ctypes.c_long * 64)()
        pid = str(os.getpid()).encode()
        if (
            ctypes.CDLL(None).fstatfs(ctypes.c_int(proc), ctypes.byref(fs)) != 0
            or fs[0] != _PROC_SUPER_MAGIC
            or os.readlink(b"self", dir_fd=proc) != pid
        ):
            raise ScratchRefusal("scratch-mount-unverified")
        self.proc = _keep(stack, _open(proc, pid, _DIR))
        self.ns_fd = _keep(
            stack, os.open(b"ns/mnt", os.O_RDONLY | os.O_CLOEXEC, dir_fd=self.proc)
        )
        if _pair(self.ns_fd) != self.namespace:
            raise ScratchRefusal("scratch-volume-binding-unverified")
        self.root_binding = _pair(self.root), _mount_id(self.proc, self.root)
        self.raw_mounts = _read(self.proc, b"mountinfo", _MOUNTINFO_BYTES)
        self.mounts = _mount_rows(self.raw_mounts)
        self.fd = self._open_home(stack)
        self.identity = _stamp(self.proc, self.fd, directory=True)
        self.recheck()

    def _open_home(self, stack):
        prefix, name = self.home.rsplit(b"/", 1)
        with ExitStack() as parents:
            parent = self.root
            for component in prefix[1:].split(b"/") if prefix else ():
                parent = _keep(parents, _open(parent, component, _DIR))
                info = os.fstat(parent)
                if info.st_uid not in (0, os.geteuid()) or (
                    info.st_mode & 0o022
                    and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
                ):
                    raise ScratchRefusal("scratch-path-unverified")
            _verified_mount(
                self.mounts, self.home, self.mount, _mount_id(self.proc, parent)
            )
            fd = _keep(stack, _open(parent, name, _DIR, cross=True))
            if (_pair(fd), _mount_id(self.proc, fd)) != (self.backing, self.mount):
                raise ScratchRefusal("scratch-volume-binding-unverified")
            return fd

    def recheck(self):
        with ExitStack() as stack:
            if threading.get_native_id() != os.getpid():
                raise ScratchRefusal("scratch-mount-changed")
            root = _keep(stack, os.open("/", _DIR | os.O_NOFOLLOW))
            namespace = _keep(
                stack, os.open(b"ns/mnt", os.O_RDONLY | os.O_CLOEXEC, dir_fd=self.proc)
            )
            if (
                _pair(namespace) != self.namespace
                or (_pair(root), _mount_id(self.proc, root)) != self.root_binding
                or _read(self.proc, b"mountinfo", _MOUNTINFO_BYTES) != self.raw_mounts
            ):
                raise ScratchRefusal("scratch-mount-changed")
            current = self._open_home(stack)
            if any(
                _stamp(self.proc, fd, directory=True) != self.identity
                for fd in (self.fd, current)
            ):
                raise ScratchRefusal("scratch-volume-changed")

    def below(self, fd, *, directory, private=False):
        stamp = _stamp(self.proc, fd, directory=directory, private=private)
        if stamp[0] != self.backing[0] or stamp[2] != self.mount:
            raise ScratchRefusal("scratch-mount-changed")
        return stamp


class _Scratch:
    def __init__(self, volume, name, fd):
        self._volume = volume
        self._name = name
        self._fd = fd
        self._identity = volume.below(fd, directory=True, private=True)
        self._directories = {}
        self._path = Path(os.fsdecode(volume.home + b"/" + name))
        self._closed = False
        self._failed = False

    def recheck(self):
        """Check the live binding; a refusal poisons it for this lifetime."""
        try:
            self._check()
        except BaseException:
            self._failed = True
            raise

    def _check(self):
        if self._closed or self._failed:
            raise ScratchRefusal("scratch-binding-ended")
        try:
            self._volume.recheck()
            with ExitStack() as stack:
                root = _keep(stack, _open(self._volume.fd, self._name, _DIR))
                if any(
                    self._volume.below(fd, directory=True, private=True)
                    != self._identity
                    for fd in (root, self._fd)
                ):
                    raise ScratchRefusal("scratch-binding-changed")
                for name, identity in self._directories.items():
                    with ExitStack() as child:
                        fd = _keep(child, _open(root, name, _DIR))
                        if (
                            self._volume.below(fd, directory=True, private=True)
                            != identity
                        ):
                            raise ScratchRefusal("scratch-binding-changed")
        except OSError:
            raise ScratchRefusal("scratch-binding-changed") from None

    def locations(self):
        """Return the rechecked scratch base, selectors, and probe data root.

        Feed selectors through the existing exact-version history environment
        fence. Native --version uses the separate probe data root, not the
        actual source data tree. The system-config/stock-binary/quiet-service
        checks still apply; this helper does not grant native launch authority.
        """
        self.recheck()
        root = self._path
        return (
            root,
            {
                "HOME": str(root / "home"),
                "OPENCODE_TEST_HOME": str(root / "home"),
                "TMPDIR": str(root / "tmp"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "OPENCODE_CONFIG_DIR": str(root / "config" / "opencode"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": os.fsdecode(self._volume.home) + "/.local/share",
                "OPENCODE_DB": "oc-box-v2.db",
            },
            str(root / "version-data"),
        )


def _absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _directory_times(fd):
    info = os.fstat(fd)
    return info.st_mtime_ns, info.st_ctime_ns


def _inventory(volume, parent, deadline, budget, depth=0):
    """Verify all candidate metadata before deleting any tree entry."""
    if depth > _MAX_DEPTH or time.monotonic() >= deadline:
        raise ScratchCleanupUncertain()
    before = _directory_times(parent)
    entries = []
    with ExitStack() as stack:
        readable = _keep(stack, _open(parent, b".", os.O_RDONLY | os.O_DIRECTORY))
        with os.scandir(readable) as listing:
            for entry in listing:
                budget[0] -= 1
                if budget[0] < 0 or time.monotonic() >= deadline:
                    raise ScratchCleanupUncertain()
                name = os.fsencode(entry.name)
                with ExitStack() as child:
                    fd = _keep(child, _open(parent, name, _META))
                    directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    identity = volume.below(fd, directory=directory)
                    nested = (
                        _inventory(volume, fd, deadline, budget, depth + 1)
                        if directory
                        else None
                    )
                    entries.append((name, identity, nested))
    if _directory_times(parent) != before:
        raise ScratchCleanupUncertain()
    return entries


def _remove(volume, parent, entries, deadline):
    for name, identity, nested in entries:
        if time.monotonic() >= deadline:
            raise ScratchCleanupUncertain()
        directory = nested is not None
        with ExitStack() as stack:
            fd = _keep(stack, _open(parent, name, _META))
            if volume.below(fd, directory=directory) != identity:
                raise ScratchCleanupUncertain()
            if directory:
                _remove(volume, fd, nested, deadline)
                current = _keep(stack, _open(parent, name, _DIR))
                if volume.below(current, directory=True) != identity:
                    raise ScratchCleanupUncertain()
                os.rmdir(name, dir_fd=parent)
            else:
                os.unlink(name, dir_fd=parent)
            if not _absent(parent, name) or os.fstat(fd).st_nlink != 0:
                raise ScratchCleanupUncertain()


def _cleanup(scratch, verify_retired):
    volume = scratch._volume
    try:
        # This is an observation of already completed process supervision, not
        # a request to wait/signal or an assumption that callback return means
        # the wrapper's exit proves remote process retirement.
        if verify_retired() is not True:
            raise ScratchCleanupUncertain()
        deadline = time.monotonic() + _CLEANUP_SECONDS
        scratch.recheck()
        entries = _inventory(volume, scratch._fd, deadline, [_MAX_ENTRIES])
        scratch.recheck()
        _remove(volume, scratch._fd, entries, deadline)
        if time.monotonic() >= deadline:
            raise ScratchCleanupUncertain()
        # The reserved directory entries have just been removed. Recheck the
        # canonical top binding before rmdir, then confirm exact absence.
        scratch._directories.clear()
        scratch.recheck()
        os.rmdir(scratch._name, dir_fd=volume.fd)
        if not _absent(volume.fd, scratch._name) or os.fstat(scratch._fd).st_nlink != 0:
            raise ScratchCleanupUncertain()
        volume.recheck()
        if time.monotonic() >= deadline:
            raise ScratchCleanupUncertain()
    except Exception:
        raise ScratchCleanupUncertain() from None
    except (KeyboardInterrupt, SystemExit) as interruption:
        interruption.history_source_cleanup_failed = True
        raise
    finally:
        scratch._closed = True


def _setup(stack, binding, verify_retired):
    if not callable(verify_retired):
        raise ScratchRefusal("scratch-profile-unverified")
    try:
        volume = _Volume(stack, _binding(binding))
        name = b".oc-box-quiet-history-" + secrets.token_hex(16).encode()
        volume.recheck()
    except ScratchRefusal:
        raise
    except (OSError, ValueError):
        raise ScratchRefusal("scratch-volume-binding-unverified") from None
    scratch = None
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=volume.fd)
        except FileExistsError:
            # A collision is not reusable, nor may we clean it up.
            raise ScratchRefusal("scratch-name-occupied") from None
        fd = _keep(stack, _open(volume.fd, name, _DIR))
        scratch = _Scratch(volume, name, fd)
        for directory in _DIRECTORIES:
            leaf = directory.encode()
            try:
                os.mkdir(leaf, 0o700, dir_fd=fd)
            except BaseException:
                # Do not let setup cleanup adopt a name whose creation was
                # not confirmed. A failed syscall may leave uncertain state.
                try:
                    if not _absent(fd, leaf):
                        scratch._failed = True
                except BaseException:
                    scratch._failed = True
                raise
            try:
                with ExitStack() as child:
                    pinned = _keep(child, _open(fd, leaf, _DIR))
                    scratch._directories[leaf] = volume.below(
                        pinned, directory=True, private=True
                    )
            except BaseException:
                scratch._failed = True
                raise
        try:
            with ExitStack() as scoped:
                home = _keep(scoped, _open(fd, b"home", _DIR))
                if (
                    volume.below(home, directory=True, private=True)
                    != scratch._directories[b"home"]
                ):
                    raise ScratchRefusal("scratch-binding-changed")
                os.mkdir(b".local", 0o700, dir_fd=home)
                fallback = _keep(scoped, _open(home, b".local", _DIR))
                scratch._directories[b"home/.local"] = volume.below(
                    fallback, directory=True, private=True
                )
        except BaseException:
            scratch._failed = True
            raise
        scratch.recheck()
        return scratch
    except BaseException as original:
        if (
            isinstance(original, ScratchRefusal)
            and str(original) == "scratch-name-occupied"
        ):
            raise
        try:
            if scratch is not None:
                # The body has not run, so no child could have been exposed.
                _cleanup(scratch, lambda: True)
            else:
                volume.recheck()
                # mkdir/open did not leave a positively identified object.
                if not _absent(volume.fd, name):
                    raise ScratchCleanupUncertain()
        except BaseException:
            if isinstance(original, (KeyboardInterrupt, SystemExit)):
                original.history_source_cleanup_failed = True
                raise original from None
            raise ScratchCleanupUncertain() from None
        if not isinstance(original, (ScratchRefusal, KeyboardInterrupt, SystemExit)):
            raise ScratchRefusal("scratch-setup-failed") from None
        raise


@contextmanager
def volume_scratch(binding, *, verify_retired):
    """Create one private lifetime below an independently admitted home volume.

    Binding must come from the trusted private control channel: ``scope`` is
    SCOPE, ``version`` is the exact admitted patch, ``home`` is the canonical
    in-container volume mount, ``device``/``inode`` are the *physical* backing
    pair, ``mount_id`` the mount ID in the container view, and ``namespace``
    the pinned mount namespace device/inode. Never derive this authority from
    native path replies, a home string or the source's ready record alone.

    verify_retired reports true only if no native process was started or all
    exact processes using the scope have been verified retired. Call it via
    this context *after* normal child supervision. Unknown retirement or drift
    preserves the suspect path and reports incomplete cleanup. A bounded
    local check cannot exclude non-cooperating same-user mutation between
    syscalls, guarantee cleanup after process death, or bound a wedged kernel.
    """
    with ExitStack() as stack:
        scratch = _setup(stack, binding, verify_retired)
        try:
            yield scratch
        except BaseException as original:
            try:
                _cleanup(scratch, verify_retired)
            except BaseException:
                # Retain the primary launch/cancellation failure, and expose
                # the independent cleanup uncertainty through the same flag
                # used by the ordinary source lifecycle.
                original.history_source_cleanup_failed = True
            raise
        else:
            _cleanup(scratch, verify_retired)
