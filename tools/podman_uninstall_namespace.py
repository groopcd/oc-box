"""Opt-in observations inside an existing *synthetic guest* Podman pause namespace."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import selectors
import signal
import stat
from threading import active_count
import time
from types import MappingProxyType


_PATH = os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW
_DIR = _PATH | os.O_DIRECTORY
_USER = 0x10000000  # CLONE_NEWUSER
_MOUNT = 0x00020000  # CLONE_NEWNS
_NS_TYPE = 0xB703  # NS_GET_NSTYPE
_PID = re.compile(rb"[1-9][0-9]{0,8}")
_NUMBER = re.compile(rb"(?:0|[1-9][0-9]{0,9})")
_PAUSE_HELPERS = ("/usr/libexec/podman/catatonit", "/usr/bin/catatonit")
_TOTAL = 40
_COMMAND = 5


class ProofRefused(ValueError):
    pass


def _require(condition):
    if not condition:
        raise ProofRefused from None


def _identity(info):
    return info.st_dev, info.st_ino


def _ready(fd, wait):
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(wait))


def _read_at(directory, name, limit):
    # Only the closed kernel proc metadata routes below use this reader.
    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=directory,
    )
    with os.fdopen(fd, "rb") as stream:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        value = stream.read(limit + 1)
        _require(type(value) is bytes and 0 < len(value) <= limit)
    return value


def _process_stamp(raw, pid):
    left, right = raw.rsplit(b") ", 1)
    prefix = str(pid).encode("ascii") + b" ("
    fields = right.split()
    _require(
        left.startswith(prefix)
        and len(fields) > 19
        and fields[0] in (b"S", b"R", b"D", b"I")
    )
    started = fields[19]
    _require(started.isdigit() and 0 < len(started) <= 20 and int(started) > 0)
    return left[len(prefix) :], int(started)


def _mappings(raw, outside):
    lines = raw.splitlines()
    _require(1 <= len(lines) <= 16)
    rows = []
    for line in lines:
        fields = line.split()
        _require(len(fields) == 3 and all(_NUMBER.fullmatch(value) for value in fields))
        start, parent, length = map(int, fields)
        _require(
            parent > 0
            and length > 0
            and max(start, parent) + length <= 2**32 - 1
            and all(
                (start + length <= a or a + n <= start)
                and (parent + length <= b or b + n <= parent)
                for a, b, n in rows
            )
        )
        rows.append((start, parent, length))
    _require(rows[0] == (0, outside, 1))
    return tuple(rows)


def _pause_executable(engine, stack, proc, name):
    observed = _identity(os.stat("exe", dir_fd=proc))
    if name == b"podman pause":
        _require(observed == engine.executable.stamp[:2])
        return engine.executable
    _require(
        name == b"catatonit" and _read_at(proc, "cmdline", 64) == b"catatonit\0-P\0"
    )
    for path in _PAUSE_HELPERS:
        try:
            pin = engine.native._pin(stack, path, 0, owner=0, directory=False)
        except FileNotFoundError:
            continue
        if (
            pin.stamp[:2] == observed
            and pin.stamp[4] & 0o111
            and not pin.stamp[4] & 0o6000
        ):
            return pin
    raise ProofRefused


@dataclass(frozen=True)
class _Pause:
    pid: int
    marker: object
    executable: object
    proc: int
    pidfd: int
    ns_dir: int
    namespaces: tuple[int, int]
    root: tuple[int, int]
    process_stamp: tuple[bytes, int]
    uid: int
    gid: int
    uid_map: tuple
    gid_map: tuple

    def check(self):
        self.marker.recheck()
        self.executable.recheck()
        _require(not _ready(self.pidfd, 0))
        live = os.stat(f"/proc/{self.pid}", follow_symlinks=False)
        _require(
            _identity(live) == _identity(os.fstat(self.proc))
            and live.st_uid == self.uid
        )
        _require(
            _process_stamp(_read_at(self.proc, "stat", 8192), self.pid)
            == self.process_stamp
        )
        _require(
            _identity(os.stat("exe", dir_fd=self.proc)) == self.executable.stamp[:2]
        )
        _require(_identity(os.stat("root", dir_fd=self.proc)) == self.root)
        for kind, fd in zip(("user", "mnt"), self.namespaces):
            _require(
                _identity(os.stat(kind, dir_fd=self.ns_dir)) == _identity(os.fstat(fd))
            )
        _require(
            _mappings(_read_at(self.proc, "uid_map", 2048), self.uid) == self.uid_map
        )
        _require(
            _mappings(_read_at(self.proc, "gid_map", 2048), self.gid) == self.gid_map
        )
        _require(not _ready(self.pidfd, 0))


@contextmanager
def _pause_fence(engine):
    _require(
        hasattr(os, "setns")
        and hasattr(os, "pidfd_open")
        and os.execve in os.supports_fd
        and os.getuid() == os.geteuid() == engine.uid > 0
        and os.getgid() == os.getegid() > 0
    )
    expected_runtime = str(engine.root / "runtime")
    _require(engine.environment.get("XDG_RUNTIME_DIR") == expected_runtime)
    with ExitStack() as stack:

        def keep(fd):
            stack.callback(os.close, fd)
            return fd

        marker = engine.native._pin(
            stack,
            expected_runtime + "/libpod/tmp/pause.pid",
            engine.uid,
            owner=engine.uid,
            directory=False,
        )
        value = engine.native._read_pin(marker, 11)
        _require(stat.S_IMODE(marker.stamp[4]) == 0o600 and _PID.fullmatch(value))
        pid = int(value)
        _require(pid > 1 and pid != os.getpid())
        procfs = keep(os.open("/proc", _DIR))
        proc = keep(os.open(str(pid), _DIR, dir_fd=procfs))
        _require(os.fstat(proc).st_uid == engine.uid)
        pidfd = keep(os.pidfd_open(pid, 0))
        process_stamp = _process_stamp(_read_at(proc, "stat", 8192), pid)
        executable = _pause_executable(engine, stack, proc, process_stamp[0])
        ns_dir = keep(os.open("ns", _DIR, dir_fd=proc))
        namespaces = []
        for kind, flag in (("user", _USER), ("mnt", _MOUNT)):
            # Only these two kernel proc namespace links may be followed.
            fd = keep(os.open(kind, os.O_RDONLY | os.O_CLOEXEC, dir_fd=ns_dir))
            _require(fcntl.ioctl(fd, _NS_TYPE) == flag)
            _require(
                _identity(os.fstat(fd)) != _identity(os.stat("/proc/self/ns/" + kind))
            )
            namespaces.append(fd)
        gid = os.getegid()
        anchor = _Pause(
            pid,
            marker,
            executable,
            proc,
            pidfd,
            ns_dir,
            tuple(namespaces),
            _identity(os.stat("root", dir_fd=proc)),
            process_stamp,
            engine.uid,
            gid,
            _mappings(_read_at(proc, "uid_map", 2048), engine.uid),
            _mappings(_read_at(proc, "gid_map", 2048), gid),
        )
        anchor.check()
        yield anchor


def _joined_path(pin):
    """Reopen path components in the entered namespace; UID/mount IDs remap."""
    fd = os.open("/", _DIR)
    try:
        parts = Path(pin.path).parts
        _require(parts[0] == "/" and ".." not in parts and len(parts) > 1)
        for index, name in enumerate(parts[1:], 2):
            flags = _DIR if index < len(parts) or pin.directory else _PATH
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        _require(
            _identity(info) == _identity(os.fstat(pin.fd)) == pin.stamp[:2]
            and info.st_mode == pin.stamp[4]
        )
    finally:
        os.close(fd)


def _join(anchor, pins):
    os.setns(anchor.namespaces[0], _USER)
    os.setns(anchor.namespaces[1], _MOUNT)
    _require(os.getresuid() == (0, 0, 0) and os.getresgid() == (0, 0, 0))
    for kind, fd in zip(("user", "mnt"), anchor.namespaces):
        _require(_identity(os.stat("/proc/self/ns/" + kind)) == _identity(os.fstat(fd)))
    _require(_identity(os.stat("/")) == anchor.root)
    proc = os.open(f"/proc/{os.getpid()}", _DIR)
    try:
        _require(
            _mappings(_read_at(proc, "uid_map", 2048), anchor.uid) == anchor.uid_map
        )
        _require(
            _mappings(_read_at(proc, "gid_map", 2048), anchor.gid) == anchor.gid_map
        )
    finally:
        os.close(proc)
    os.chdir("/")
    for pin in pins:
        _joined_path(pin)


def _seconds(deadline):
    left = deadline - time.monotonic()
    _require(left > 0)
    return left


def _capture(anchor, binary, argv, environment, pins, deadline, limit):
    # Single-threaded, fixed guest diagnostic only. Direct exec avoids a
    # shell, an unshare wrapper, and an unbounded preexec handshake.
    _require(active_count() == 1)
    with os.scandir("/proc/self/task") as tasks:
        _require(next(tasks, None) is not None and next(tasks, None) is None)
    _seconds(deadline)
    with ExitStack() as stack:

        def keep(fd):
            stack.callback(os.close, fd)
            return fd

        read, write = os.pipe2(os.O_CLOEXEC)
        keep(read)
        with os.fdopen(write, "wb", buffering=0) as writer:
            null = keep(os.open("/dev/null", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC))
            child = os.fork()
            if child == 0:
                try:
                    os.dup2(null, 0)
                    os.dup2(write, 1)
                    os.dup2(null, 2)
                    _join(anchor, pins)
                    os.execve(binary.fd, (binary.path, *argv), environment)
                except BaseException:
                    os._exit(125)
        # Always reap the exact child. Never signal the pause process or a
        # process group; no retry or inference if a direct reap is uncertain.
        reaped = False
        try:
            pidfd = keep(os.pidfd_open(child, 0))
            os.set_blocking(read, False)
            raw = bytearray()
            while True:
                _require(_ready(read, _seconds(deadline)))
                try:
                    data = os.read(read, min(65536, limit + 1 - len(raw)))
                except BlockingIOError:
                    continue
                if not data:
                    break
                raw.extend(data)
                _require(len(raw) <= limit)
            _require(_ready(pidfd, _seconds(deadline)))
            got, status = os.waitpid(child, os.WNOHANG)
            reaped = got == child
            _require(reaped and os.waitstatus_to_exitcode(status) == 0)
            return bytes(raw)
        finally:
            if not reaped:
                got, _ = os.waitpid(child, os.WNOHANG)
                if got != child:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    cutoff = time.monotonic() + 2
                    while os.waitpid(child, os.WNOHANG)[0] != child:
                        time.sleep(min(0.01, _seconds(cutoff)))


def _store_observations(engine, snapshot):
    native = engine.native
    _require(
        len(snapshot.containers) == 2
        and len(snapshot.volumes) == 1
        and not snapshot.complete
    )
    ids = tuple(sorted(item.ident for item in snapshot.containers))
    volume = snapshot.volumes[0]
    _require(volume.mountpoint is not None and volume.mount_count == 0)
    allowed = (
        native._INFO,
        native._PS,
        (*native._PS, "--external"),
        native._MOUNT_LIST,
        ("container", "inspect", "--", *ids),
        ("volume", "inspect", "--", volume.name),
    )
    _require(all(native._read_only_query(argv) for argv in allowed))
    return ids, volume, allowed


def _verify_catalog(engine, snapshot, ids, volume, query):
    native = engine.native
    info = native._json(query(native._INFO))
    _require(
        native._bound_profile(info, engine.roots, engine.scope_conf)
        == (snapshot.profile, 2)
    )
    images = info["store"].get("imageStore")
    _require(
        type(images) is dict
        and set(images) == {"number"}
        and type(images["number"]) is int
        and images["number"] == 1
    )
    for argv in (native._PS, (*native._PS, "--external")):
        _require(native._catalog(query(argv), native.review._ENGINE_ID) == ids)
    known = {item.ident: item for item in snapshot.containers}
    for item in native._listed(query(("container", "inspect", "--", *ids)), "Id", ids):
        row = known[item["Id"]]
        state = item.get("State")
        deps = item.get("Dependencies")
        config = item.get("Config")
        _require(
            item.get("Name") == row.name
            and item.get("Created") == row.created_at
            and item.get("Pod") == item.get("Namespace") == item.get("Rootfs") == ""
            and type(deps) is list
            and all(type(dep) is str for dep in deps)
            and tuple(sorted(deps)) == row.dependencies
            and type(config) is dict
            and "Labels" in config
            and native._labels(config["Labels"]) == (row.workspace_label, row.managed)
            and type(state) is dict
            and state.get("Status") == row.status
            and state.get("Dead") is False
            and state.get("Running") is row.running
            and state.get("Paused") is row.paused
            and state.get("Restarting") is row.restarting
            and type(state.get("Pid")) is int
            and state["Pid"] == row.pid
            and type(state.get("ConmonPid", 0)) is int
            and state.get("ConmonPid", 0) == row.conmon_pid
            and item.get("GraphDriver", {}).get("Data", {}).get("UpperDir")
            == row.writable.path
        )
    (home,) = native._listed(
        query(("volume", "inspect", "--", volume.name)), "Name", (volume.name,)
    )
    _require(
        home.get("Driver") == "local"
        and home.get("Scope") == "local"
        and home.get("Mountpoint") == volume.mountpoint.path
        and home.get("CreatedAt") == volume.created_at
        and type(home.get("MountCount")) is int
        and home["MountCount"] == volume.mount_count
        and home.get("Options") == {}
        and native._labels(home.get("Labels"))[0] == volume.workspace_label
        and home.get("Anonymous", False) is False
        and home.get("StorageID", "") == ""
    )


def probe(engine, snapshot, *, fence=_pause_fence, capture=_capture):
    """Add separate guest diagnostic facts; never replace the direct snapshot."""
    result = {
        "namespace_probe": "namespace_unavailable",
        "namespace_context_verified": False,
        "namespace_both_observed_unmounted": False,
    }
    try:
        _require(engine.phase == "read-only")
        engine.check()
        ids, volume, allowed = _store_observations(engine, snapshot)
        deadline = min(engine.deadline, time.monotonic() + _TOTAL)
        with fence(engine) as anchor, ExitStack() as stack:
            result["namespace_probe"] = "namespace_unverified"
            _require(anchor.uid == engine.uid and anchor.gid > 0)
            storage = (
                *[item.writable for item in snapshot.containers],
                volume.mountpoint,
            )
            pins = list(engine.pins)
            for path in storage:
                pin = engine.native._pin(
                    stack, path.path, engine.uid, owner=engine.uid, directory=True
                )
                _require(pin.stamp[:3] == path.identity)
                pins.append(pin)
            environment = MappingProxyType(
                {
                    **engine.environment,
                    "_CONTAINERS_USERNS_CONFIGURED": "done",
                    "_CONTAINERS_ROOTLESS_UID": str(anchor.uid),
                    "_CONTAINERS_ROOTLESS_GID": str(anchor.gid),
                }
            )

            def recheck():
                _seconds(deadline)
                engine.check()
                anchor.check()
                for pin in pins:
                    pin.recheck()

            def query(argv):
                _require(argv in allowed)
                recheck()
                limit = (
                    engine.native._INSPECT
                    if argv[1:2] == ("inspect",)
                    else engine.native._SMALL
                )
                output = capture(
                    anchor,
                    engine.executable,
                    (*engine.prefix, *argv),
                    environment,
                    tuple(pins),
                    min(deadline, time.monotonic() + _COMMAND),
                    limit,
                )
                recheck()
                _require(type(output) is bytes and 0 < len(output) <= limit)
                return output

            def mounted():
                return engine.native._mount_catalog(
                    query(engine.native._MOUNT_LIST),
                    snapshot.containers,
                    engine.roots[:2],
                )

            _verify_catalog(engine, snapshot, ids, volume, query)
            first = mounted()
            _require(mounted() == first)
            _verify_catalog(engine, snapshot, ids, volume, query)
            _require(engine.scan(deadline=deadline) == snapshot)
            recheck()
            reported = dict(first)
            _require(
                all(
                    item.mounted is None or item.mounted is (item.ident in reported)
                    for item in snapshot.containers
                )
            )
        result.update(
            namespace_probe="namespace_observed",
            namespace_context_verified=True,
            namespace_both_observed_unmounted=not first,
        )
    except (Exception, KeyboardInterrupt):
        pass
    return result
