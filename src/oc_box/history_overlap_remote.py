"""Host process-identity witness for a guarded, one-child history exec.

No runtime commands, process signals, launch authority, remote effects, or
whole-container stop inference. The caller owns the verified local engine and
original resource, the private control channel, and all lifecycle fences.
"""

from contextlib import ExitStack
import ctypes
from dataclasses import dataclass
import fcntl
import os
import re
import select
import stat
from threading import current_thread
import time
from typing import Callable

if __package__:
    from .history_overlap_io import HelperAbsent, SourceAttempt
else:
    from history_overlap_io import HelperAbsent, SourceAttempt


TOP_COLUMNS = ("pid", "hpid")
MAX_TOP_BYTES = 65536
MAX_READY_BYTES = 160
_MAX_ROWS = 4096
_MAX_PID = 2**31 - 1
_MAX_TICKS = 2**64 - 1
# Linux UAPI pidfd.h: _IO(0xff, 5) and _IO(0xff, 3) on these ABIs.
_PIDFD_NAMESPACES = (0xFF05, 0xFF03)  # PID, mount
_IOCTL_ABIS = frozenset(("x86_64", "aarch64"))
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEAF = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_ALIVE = frozenset((b"R", b"S", b"D", b"T", b"t", b"W", b"P", b"I"))
_READY = re.compile(
    rb"OC-BOX-HISTORY-READY/1 ([a-f0-9]{32}) "
    rb"([1-9][0-9]{0,9}) ([1-9][0-9]{0,19}) "
    rb"([1-9][0-9]{0,9}) ([1-9][0-9]{0,19})\n"
)


class RemoteWitnessError(RuntimeError):
    """Fixed refusal category; never contains process or probe output."""


@dataclass(frozen=True, slots=True, repr=False)
class TopSnapshot:
    """One bounded private read of ``podman top <exact-id> pid hpid``.

    The trusted probe verifies the exact local engine and original resource
    generation including the running init identity, before and after the
    query. It must enforce the deadline and byte cap during the query and
    timestamp this fresh result in the host's monotonic clock. No remote
    engine, cached result, partial output, or fallback ps format qualifies.
    """

    attempt: SourceAttempt
    init_host_pid: int
    init_start_ticks: int
    observed_ns: int
    output: bytes


def _number(raw, upper, *, zero=False):
    if not re.fullmatch(rb"0|[1-9][0-9]{0,19}", raw):
        raise ValueError()
    value = int(raw)
    if not (0 if zero else 1) <= value <= upper:
        raise ValueError()
    return value


def _ready(attempt, record):
    match = (
        _READY.fullmatch(record)
        if type(record) is bytes and len(record) <= MAX_READY_BYTES
        else None
    )
    if match is None or match[1] != attempt.token.encode("ascii"):
        raise RemoteWitnessError("remote-ready")
    try:
        controller = (_number(match[2], _MAX_PID), _number(match[3], _MAX_TICKS))
        child = (_number(match[4], _MAX_PID), _number(match[5], _MAX_TICKS))
    except ValueError:
        raise RemoteWitnessError("remote-ready") from None
    if min(controller[0], child[0]) <= 1 or controller[0] == child[0]:
        raise RemoteWitnessError("remote-ready")
    return controller, child


def _top(output):
    if (
        type(output) is not bytes
        or not 1 <= len(output) <= MAX_TOP_BYTES
        or not output.endswith(b"\n")
    ):
        raise RemoteWitnessError("remote-top-layout")
    head, *rows = output[:-1].split(b"\n")
    if not re.fullmatch(rb"[ \t]*PID[ \t]+HPID[ \t]*", head) or not (
        1 <= len(rows) <= _MAX_ROWS
    ):
        raise RemoteWitnessError("remote-top-layout")
    mapped, host = {}, set()
    for row in rows:
        match = re.fullmatch(rb"[ \t]*([0-9]{1,10})[ \t]+([0-9]{1,10})[ \t]*", row)
        if match is None:
            raise RemoteWitnessError("remote-top-layout")
        try:
            pid, hpid = (_number(raw, _MAX_PID) for raw in match.groups())
        except ValueError:
            raise RemoteWitnessError("remote-top-layout") from None
        if pid in mapped or hpid in host:
            raise RemoteWitnessError("remote-top-layout")
        mapped[pid] = hpid
        host.add(hpid)
    return mapped


def _check_deadline(deadline, *, initial=False):
    now = time.monotonic()
    if (
        type(deadline) not in (int, float)
        or not now < deadline
        or (initial and deadline > now + 5)
    ):
        raise RemoteWitnessError("remote-deadline")


def _probe(attempt, probe, deadline):
    _check_deadline(deadline)
    before = time.monotonic_ns()
    try:
        sample = probe(attempt, deadline, MAX_TOP_BYTES)
    except Exception:
        raise RemoteWitnessError("remote-top-probe") from None
    _check_deadline(deadline)
    if (
        type(sample) is not TopSnapshot
        or sample.attempt is not attempt
        or type(sample.observed_ns) is not int
        or not before <= sample.observed_ns <= time.monotonic_ns()
        or type(sample.init_host_pid) is not int
        or not 1 <= sample.init_host_pid <= _MAX_PID
        or type(sample.init_start_ticks) is not int
        or not 1 <= sample.init_start_ticks <= _MAX_TICKS
    ):
        raise RemoteWitnessError("remote-top-probe")
    mapping = _top(sample.output)
    if mapping.get(1) != sample.init_host_pid:
        raise RemoteWitnessError("remote-original")
    return sample, mapping


def _open_proc(stack):
    fd = os.open("/proc", _DIR)
    stack.callback(os.close, fd)
    # Linux struct statfs starts with long f_type. Supply an aligned buffer
    # larger than the Linux ABI struct; no fields past f_type are interpreted.
    buf = (ctypes.c_long * 64)()
    try:
        fstatfs = ctypes.CDLL(None, use_errno=True).fstatfs
    except AttributeError:
        raise RemoteWitnessError("remote-proc") from None
    if fstatfs(fd, ctypes.byref(buf)) != 0 or buf[0] != 0x9FA0:
        raise RemoteWitnessError("remote-proc")
    if os.readlink("self", dir_fd=fd) != str(os.getpid()):
        raise RemoteWitnessError("remote-proc")
    return fd


def _read_leaf(directory, name, maximum):
    fd = os.open(name, _LEAF, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError()
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(fd, min(4096, maximum + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise ValueError()
    finally:
        os.close(fd)


def _stat_fields(data, host_pid):
    head, tail, fields = data.rpartition(b") ")
    if not tail or not head.startswith(str(host_pid).encode() + b" ("):
        raise ValueError()
    values = fields.split()
    if len(values) <= 19 or values[0] not in _ALIVE:
        raise ValueError()
    return _number(values[1], _MAX_PID, zero=True), _number(values[19], _MAX_TICKS)


def _pid_vector(data, host_pid):
    lines = [row for row in data.split(b"\n") if row.startswith(b"NSpid:")]
    if len(lines) != 1:
        raise ValueError()
    raw = lines[0].removeprefix(b"NSpid:").split()
    if not 1 <= len(raw) <= 32:
        raise ValueError()
    ids = tuple(_number(value, _MAX_PID) for value in raw)
    if ids[0] != host_pid:
        raise ValueError()
    return ids


def _namespaces(directory):
    fd = os.open("ns", _DIR, dir_fd=directory)
    try:
        ids = []
        for name in (b"pid", b"mnt"):
            # Proc namespace entries are kernel magic links. Inspect their
            # canonical labels without following the links to another path.
            value = os.readlink(name, dir_fd=fd)
            match = re.fullmatch(name + rb":\[([1-9][0-9]{0,19})\]", value)
            if match is None:
                raise ValueError()
            ids.append(_number(match[1], _MAX_TICKS))
        return tuple(ids)
    finally:
        os.close(fd)


def _poll(pidfds, milliseconds=0):
    poller = select.poll()
    for fd in pidfds:
        poller.register(fd, select.POLLIN)
    ended = set()
    for fd, bits in poller.poll(milliseconds):
        if not bits & select.POLLIN or bits & ~(select.POLLIN | select.POLLHUP):
            raise RemoteWitnessError("remote-pidfd")
        ended.add(fd)
    return ended


@dataclass(frozen=True, slots=True, repr=False)
class _Process:
    host_pid: int
    pidfd: int
    directory: int

    def live(self):
        if _poll((self.pidfd,)):
            raise RemoteWitnessError("remote-process-exited")
        first = _stat_fields(_read_leaf(self.directory, "stat", 4096), self.host_pid)
        ids = _pid_vector(_read_leaf(self.directory, "status", 65536), self.host_pid)
        namespaces = _namespaces(self.directory)
        last = _stat_fields(_read_leaf(self.directory, "stat", 4096), self.host_pid)
        if first != last or _poll((self.pidfd,)):
            raise RemoteWitnessError("remote-process-drift")
        return first, ids, namespaces


def _pin(stack, proc, host_pid):
    pidfd = os.pidfd_open(host_pid)
    stack.callback(os.close, pidfd)
    if _poll((pidfd,)):
        raise RemoteWitnessError("remote-process-exited")
    # A live pidfd on both sides of the no-follow proc read rules out numeric
    # PID recycling during association with the proc directory.
    directory = os.open(str(host_pid), _DIR, dir_fd=proc)
    stack.callback(os.close, directory)
    return _Process(host_pid, pidfd, directory)


def _pin_namespaces(stack, process):
    """Keep the original namespace objects alive without following magic links."""
    try:
        ids = []
        for request in _PIDFD_NAMESPACES:
            fd = fcntl.ioctl(process.pidfd, request)
            stack.callback(os.close, fd)
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode) or os.get_inheritable(fd):
                raise RemoteWitnessError("remote-namespace")
            ids.append(status.st_ino)
        return tuple(ids)
    except OSError:
        raise RemoteWitnessError("remote-namespace") from None


class RemoteWitness:
    """Pinned proof producer for one controller and its sole direct child."""

    @classmethod
    def acquire(
        cls,
        attempt: SourceAttempt,
        ready: bytes,
        *,
        probe: Callable[[SourceAttempt, float, int], TopSnapshot],
        deadline: float,
    ):
        """Bind while the private one-shot exec is waiting at its READY barrier.

        ``ready`` is one exact ASCII record ``OC-BOX-HISTORY-READY/1 <token>
        <controller-pid> <controller-start> <child-pid> <child-start>\\n``,
        with single spaces and ticks from proc stat field 22. Only an audited
        fixed controller may emit it, on the just-launched attempt's private
        channel. It must have already created its sole direct child; both must
        remain alive and blocked until explicit admission from the owner.
        EOF or refusal must cancel, not release the barrier. No forks outside
        this pair, daemonization, reparenting, namespace changes, or replacement
        launch are allowed. A one-way request/result transport does not qualify.

        The owner releases the barrier separately under its short admission
        gate, after checking stop/identity and calling ``check_ready`` again,
        by sending ``OC-BOX-HISTORY-GO/1 <token>\\n`` on the private channel.
        This witness does not send it. There must be no second READY or GO
        for an attempt. ``probe(attempt, absolute_deadline, byte_limit)``
        must honor both bounds and return an independently verified snapshot.
        No fallback is available when the kernel denies or lacks pidfd
        namespace ioctls, private namespaces, or the required proc metadata.
        """
        _check_deadline(deadline, initial=True)
        if type(attempt) is not SourceAttempt or not callable(probe):
            raise RemoteWitnessError("remote-binding")
        controller_id, child_id = _ready(attempt, ready)
        if not hasattr(os, "pidfd_open") or os.uname().machine not in _IOCTL_ABIS:
            raise RemoteWitnessError("remote-unsupported")
        sample, mapping = _probe(attempt, probe, deadline)
        if controller_id[0] not in mapping or child_id[0] not in mapping:
            raise RemoteWitnessError("remote-mapping")
        with ExitStack() as stack:
            try:
                proc = _open_proc(stack)
                owner = _pin(stack, proc, os.getpid())
                _, owner_pids, owner_ns = owner.live()
                if owner_pids != (os.getpid(),):
                    raise RemoteWitnessError("remote-proc")
                init = _pin(stack, proc, sample.init_host_pid)
                controller = _pin(stack, proc, mapping[controller_id[0]])
                child = _pin(stack, proc, mapping[child_id[0]])
                init_live, control_live, child_live = (
                    process.live() for process in (init, controller, child)
                )
                _, init_ids, namespaces = init_live
                if (
                    len(init_ids) <= 1
                    or namespaces[0] == owner_ns[0]
                    or namespaces[1] == owner_ns[1]
                    # Keep both exact namespace objects alive through
                    # retirement, so their proc inode labels cannot be reused.
                    or _pin_namespaces(stack, init) != namespaces
                ):
                    raise RemoteWitnessError("remote-namespace")
                if init_live[0][1] != sample.init_start_ticks or init_ids[-1] != 1:
                    raise RemoteWitnessError("remote-original")
                for (inner, ticks), info in (
                    (controller_id, control_live),
                    (child_id, child_live),
                ):
                    if info[2] != namespaces or len(info[1]) != len(init_ids):
                        raise RemoteWitnessError("remote-namespace")
                    if info[1][-1] != inner or info[0][1] != ticks:
                        raise RemoteWitnessError("remote-process-drift")
                if child_live[0][0] != controller.host_pid:
                    raise RemoteWitnessError("remote-parent")
                # Recheck all three bindings before permitting eventual
                # release; a terminal/reparented/moved process fails closed.
                bindings = (
                    (init, init_live),
                    (controller, control_live),
                    (child, child_live),
                )
                for process, info in bindings:
                    if process.live() != info:
                        raise RemoteWitnessError("remote-process-drift")
                _check_deadline(deadline)
            except (OSError, ValueError):
                raise RemoteWitnessError("remote-proc") from None
            witness = cls()
            witness._owner = (os.getpid(), current_thread())
            witness._attempt, witness._probe = attempt, probe
            witness._init, witness._init_live = init, init_live
            witness._mapped = (
                (controller_id[0], controller.host_pid),
                (child_id[0], child.host_pid),
            )
            witness._workers = (controller.pidfd, child.pidfd)
            witness._bindings = bindings
            witness._resources = stack.pop_all()
            return witness

    def _thread(self):
        if (os.getpid(), current_thread()) != self._owner:
            raise RemoteWitnessError("remote-owner")

    def __enter__(self):
        self._thread()
        return self

    def __exit__(self, _kind, _error, _traceback):
        self.close()

    def close(self):
        self._thread()
        if self._resources is not None:
            resources, self._resources = self._resources, None
            try:
                resources.close()
            except OSError:
                raise RemoteWitnessError("remote-proc") from None

    def check_ready(self, *, deadline: float):
        """Recheck the three live bindings immediately before release.

        Bounded proc-only checks: no engine call and no dispatch. The owner
        still holds its own checked admission gate. Failure invalidates this
        witness; it must not be used to release or infer helper absence.
        """
        self._thread()
        if self._resources is None:
            raise RemoteWitnessError("remote-owner")
        try:
            _check_deadline(deadline, initial=True)
            for process, info in self._bindings:
                if process.live() != info:
                    raise RemoteWitnessError("remote-process-drift")
                _check_deadline(deadline)
        except (OSError, ValueError):
            self.close()
            raise RemoteWitnessError("remote-proc") from None
        except BaseException:
            self.close()
            raise

    def observe(self, *, deadline: float) -> HelperAbsent | None:
        """After local retirement, wait boundedly for both exact pidfds.

        A live process at the deadline yields None. Once both are terminal,
        require a fresh exact-original probe, unchanged living init binding,
        and no old PID slot in the mapping. Any identity or probe refusal
        invalidates the witness. Not a complete-container stop observation.
        """
        self._thread()
        if self._resources is None:
            raise RemoteWitnessError("remote-owner")
        _check_deadline(deadline, initial=True)
        try:
            waiting = set(self._workers)
            while waiting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                waiting -= _poll(waiting, min(50, max(1, int(remaining * 1000))))
            sample, mapping = _probe(self._attempt, self._probe, deadline)
            if (
                sample.init_host_pid != self._init.host_pid
                or sample.init_start_ticks != self._init_live[0][1]
                or self._init.live() != self._init_live
                or any(
                    inner in mapping or host in mapping.values()
                    for inner, host in self._mapped
                )
            ):
                raise RemoteWitnessError("remote-original")
            if _poll(self._workers) != set(self._workers):
                raise RemoteWitnessError("remote-pidfd")
            _check_deadline(deadline)
            return HelperAbsent(self._attempt, time.monotonic_ns())
        except (OSError, ValueError):
            self.close()
            raise RemoteWitnessError("remote-proc") from None
        except BaseException:
            self.close()
            raise
