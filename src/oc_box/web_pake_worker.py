"""Private Linux process transport for a trusted, one-exchange PAKE verifier.

Nothing here selects a backend, interprets passwords or derives suite bindings.
The caller pins an independently reviewed verifier and loader/runtime, supplies
the exact suite sizes and IDs, and provides its own aggregate admission budget.
The child must not fork, open other channels or persist state; this transport
does not sandbox an executable, protect against a same-user debugger or erase
all copies of protocol data.
"""

import fcntl
import hashlib
import math
import os
import select
import signal
import stat
import subprocess
from dataclasses import dataclass
from threading import Event, Lock, Timer
from time import monotonic, sleep

_V1 = b"OCOP\x01"
_START = _V1 + b"\x01"
_FINISH = _V1 + b"\x02"
_OK = b"\x00"
_REFUSE = b"\x01"
_ID_SIZE = 32  # All three IDs and the context digest.
_MAX_FIELD = 4096
_MAX_EXECUTABLE = 32 * 1024 * 1024
_POLL = 0.025
_EOF_GRACE = 0.04
_REAP_BUDGET = 0.5
_DRAIN_BUDGET = 0.75

# Applying limits in a fresh interpreter avoids preexec_fn in a threaded relay.
# Only the sealed executable FD survives the bootstrap exec, and fd-exec closes
# it before entering the verifier. No shell, PATH lookup or protocol argv/env.
_CHILD_ENTRY = """
import os, resource, sys
fd = int(sys.argv[1])
os.set_inheritable(fd, False)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execve(fd, sys.argv[2:], {})
"""
_VERIFIER_ARGV = ("oc-box-web-pake-worker",)


class PakeWorkerError(Exception):
    """Fixed worker refusal; do not disclose protocol or child diagnostics."""

    def __init__(self):
        super().__init__("Web PAKE worker refused.")


class PakeWorkerCleanupError(PakeWorkerError):
    """A verifier may still be live; the caller must keep its teardown fence."""


@dataclass(frozen=True, slots=True)
class WorkerSizes:
    """Fixed `OCOP` v1 field widths from a trusted suite, not from the wire."""

    setup: int
    registration: int
    ke1: int
    ke2: int
    ke3: int

    def __post_init__(self):
        for size in (self.setup, self.registration, self.ke1, self.ke2, self.ke3):
            if type(size) is not int or not 0 < size <= _MAX_FIELD:
                raise PakeWorkerError()


LAB_SIZES = WorkerSizes(setup=128, registration=192, ke1=96, ke2=320, ke3=64)


def _width(value, size):
    return type(value) is bytes and len(value) == size


def _close_fd(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _dup(fd):
    return fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)


def _verify_executable(fd, expected):
    seals = (
        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
    )
    if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals:
        raise PakeWorkerError()
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or info.st_mode & 0o7000
        or not 4 < info.st_size <= _MAX_EXECUTABLE
        or os.pread(fd, 4, 0) != b"\x7fELF"
    ):
        raise PakeWorkerError()
    digest = hashlib.sha256()
    offset = 0
    while offset < info.st_size:
        chunk = os.pread(fd, min(65536, info.st_size - offset), offset)
        if not chunk:
            raise PakeWorkerError()
        digest.update(chunk)
        offset += len(chunk)
    if digest.digest() != expected:
        raise PakeWorkerError()


def _require_private_reaping():
    # A zero/synthetic subprocess status is not proof of the verifier's exit.
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise PakeWorkerError()


def _spawn(fd):
    return subprocess.Popen(
        (
            "/proc/self/exe",
            "-I",
            "-S",
            "-B",
            "-c",
            _CHILD_ENTRY,
            str(fd),
            *_VERIFIER_ARGV,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(fd,),
        bufsize=0,
        cwd="/",
        env={},
        start_new_session=True,
    )


class PakeWorker:
    """Start at most one native process per exchange, with bounded live slots.

    ``executable_fd`` is a borrowed fully sealed executable ELF descriptor,
    and ``expected_sha256`` is the trusted raw digest of that exact artifact.
    The worker duplicates the FD and never resolves a verifier name or path.
    This requires Linux descriptor execution/pidfds and sole ownership of child
    reaping (no process-global wait-any reaper). Call ``close`` for teardown.
    """

    def __init__(
        self,
        executable_fd: int,
        *,
        expected_sha256: bytes,
        sizes: WorkerSizes,
        max_workers: int = 8,
        io_timeout: float = 2,
        attempt_timeout: float = 30,
    ):
        if (
            type(executable_fd) is not int
            or executable_fd < 3
            or not _width(expected_sha256, 32)
            or type(sizes) is not WorkerSizes
            or type(max_workers) is not int
            or not 0 < max_workers <= 16
            or type(io_timeout) not in (float, int)
            or not 0 < io_timeout <= 10
            or type(attempt_timeout) not in (float, int)
            or not 0 < attempt_timeout <= 60
        ):
            raise PakeWorkerError()
        self._sizes = sizes
        self._limit = max_workers
        self._io_timeout = io_timeout
        self._ttl = attempt_timeout
        self._lock = Lock()
        self._active: set[PakeWorkerAttempt] = set()
        self._closed = self._fenced = False
        fd = probe = None
        try:
            _require_private_reaping()
            if os.execve not in os.supports_fd:
                raise PakeWorkerError()
            probe = os.pidfd_open(os.getpid())
            signal.pidfd_send_signal(probe, 0)
            fd = _dup(executable_fd)
            _verify_executable(fd, expected_sha256)
            self._fd, fd = fd, None
        except (AttributeError, OSError, OverflowError, ValueError):
            raise PakeWorkerError() from None
        finally:
            _close_fd(probe)
            _close_fd(fd)

    def start(
        self,
        *,
        setup: bytes,
        registration: bytes | None,
        credential_identifier: bytes,
        client_identifier: bytes,
        verifier_identifier: bytes,
        context: bytes,
        ke1: bytes,
        deadline: float | None = None,
    ) -> tuple[bytes, "PakeWorkerAttempt"]:
        """Send START and return KE2 and an owned, nonserialized handle.

        The IDs and ``context`` must already be exactly 32 bytes, from the
        caller's pinned profile. ``deadline`` may shorten the absolute
        monotonic lifetime. ``None`` registration selects the fake-record slot;
        actual record decoding and proof verification are the child's job.
        """
        sizes = self._sizes
        if (
            not _width(setup, sizes.setup)
            or (
                registration is not None
                and not _width(registration, sizes.registration)
            )
            or not all(
                _width(value, _ID_SIZE)
                for value in (
                    credential_identifier,
                    client_identifier,
                    verifier_identifier,
                    context,
                )
            )
            or not _width(ke1, sizes.ke1)
            or (
                deadline is not None
                and (type(deadline) not in (int, float) or not 0 < deadline < math.inf)
            )
        ):
            raise PakeWorkerError()
        frame = b"".join(
            (
                _START,
                setup,
                b"\x00" if registration is None else b"\x01",
                bytes(sizes.registration) if registration is None else registration,
                credential_identifier,
                client_identifier,
                verifier_identifier,
                context,
                ke1,
            )
        )
        now = monotonic()
        cutoff = now + self._ttl
        if deadline is not None:
            cutoff = min(cutoff, deadline)
        attempt = PakeWorkerAttempt(self, cutoff)
        try:
            with self._lock:
                if (
                    self._closed
                    or self._fenced
                    or len(self._active) >= self._limit
                    or now >= cutoff
                ):
                    raise PakeWorkerError()
                fd = _dup(self._fd)
                self._active.add(attempt)
            try:
                return attempt._begin(fd, frame), attempt
            finally:
                _close_fd(fd)
        except PakeWorkerCleanupError:
            raise
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            raise PakeWorkerError() from None

    def _release(self, attempt):
        with self._lock:
            self._active.discard(attempt)

    def _fence(self):
        with self._lock:
            self._fenced = True

    def _allows_result(self):
        with self._lock:
            return not (self._closed or self._fenced)

    def close(self) -> bool:
        """Cancel all attempts; true only when none still need verified reaping.

        False is a fixed cleanup uncertainty, not permission to release the
        owner. Another close may recheck cleanup of the same child, never retry
        a proof or start a replacement process.
        """
        deadline = monotonic() + _DRAIN_BUDGET
        with self._lock:
            self._closed = True
            _close_fd(self._fd)
            self._fd = None
            attempts = tuple(self._active)
            for attempt in attempts:
                attempt._cancel.set()
        for attempt in attempts:
            attempt._discard_until(deadline)
        with self._lock:
            return not self._active


class PakeWorkerAttempt:
    """One private exchange. Only the current I/O owner touches process FDs."""

    def __init__(self, owner, deadline):
        self._owner = owner
        self._deadline = deadline
        self._cancel = Event()
        self._io = Lock()
        self._timer = None
        self._child = None
        self._pidfd = None
        self._used = False
        self._released = False

    def _check(self, deadline):
        if self._cancel.is_set() or monotonic() >= deadline:
            raise PakeWorkerError()

    def _poll(self, fd, mask, deadline):
        poller = select.poll()
        poller.register(fd, mask)
        self._check(deadline)
        poller.poll(math.ceil(1000 * min(_POLL, max(0, deadline - monotonic()))))

    def _write(self, data, deadline):
        fd = self._child.stdin.fileno()
        remaining = memoryview(data)
        while remaining:
            self._check(deadline)
            try:
                sent = os.write(fd, remaining)
            except BlockingIOError:
                self._poll(fd, select.POLLOUT, deadline)
                continue
            if not sent:
                raise PakeWorkerError()
            remaining = remaining[sent:]

    def _read(self, size, deadline):
        fd = self._child.stdout.fileno()
        while True:
            self._check(deadline)
            try:
                return os.read(fd, size)
            except BlockingIOError:
                self._poll(fd, select.POLLIN, deadline)

    def _exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            chunk = self._read(size - len(data), deadline)
            if not chunk:
                raise PakeWorkerError()
            data.extend(chunk)
        return bytes(data)

    def _status(self, opcode, deadline):
        header = self._exact(len(opcode) + 1, deadline)
        if header[:-1] != opcode or header[-1:] not in (_OK, _REFUSE):
            raise PakeWorkerError()
        return header[-1:]

    def _expect_pending(self, deadline):
        # No bytes/EOF may already be queued before FINISH. Once FINISH has
        # been sent, the trusted verifier owns the causal ACK-after-proof rule.
        self._check(deadline)
        try:
            os.read(self._child.stdout.fileno(), 1)
        except BlockingIOError:
            if self._wait_exit(monotonic()) is None:
                return
        raise PakeWorkerError()

    def _begin(self, fd, frame):
        with self._io:
            pending = False
            deadline = min(self._deadline, monotonic() + self._owner._io_timeout)
            try:
                self._check(deadline)
                _require_private_reaping()
                self._child = _spawn(fd)
                self._pidfd = os.pidfd_open(self._child.pid)
                os.set_blocking(self._child.stdin.fileno(), False)
                os.set_blocking(self._child.stdout.fileno(), False)
                self._write(frame, deadline)
                if self._status(_START, deadline) != _OK:
                    raise PakeWorkerError()
                ke2 = self._exact(self._owner._sizes.ke2, deadline)
                self._expect_pending(deadline)
                self._timer = Timer(max(0, self._deadline - monotonic()), self.discard)
                self._timer.daemon = True
                self._timer.start()
                self._check(deadline)
                pending = True
                return ke2
            finally:
                if not pending:
                    self._cleanup(monotonic() + _REAP_BUDGET)
                    if not self._released:
                        raise PakeWorkerCleanupError() from None

    def finish(self, ke3: bytes) -> bool:
        """Consume the handle; true requires exact ACK, EOF and clean waitpid."""
        if not self._io.acquire(blocking=False):
            return False
        try:
            if self._used:
                return False
            self._used = True
            deadline = min(self._deadline, monotonic() + self._owner._io_timeout)
            try:
                if not _width(ke3, self._owner._sizes.ke3):
                    return False
                self._expect_pending(deadline)
                self._write(_FINISH + ke3, deadline)
                self._child.stdin.close()
                status = self._status(_FINISH, deadline)
                if self._read(1, deadline):
                    return False
                verified = (
                    self._wait_exit(deadline, cancellable=True) == 0 and status == _OK
                )
            except (OSError, PakeWorkerError, ValueError):
                return False
            finally:
                self._cleanup(monotonic() + _REAP_BUDGET)
            return (
                verified
                and monotonic() < deadline
                and not self._cancel.is_set()
                and self._owner._allows_result()
            )
        finally:
            self._io.release()

    def discard(self) -> bool:
        """Idempotently cancel; report whether cleanup was confirmed."""
        self._cancel.set()
        return self._discard_until(monotonic() + _DRAIN_BUDGET)

    def _discard_until(self, deadline):
        if not self._io.acquire(timeout=max(0, deadline - monotonic())):
            return False
        try:
            self._cleanup(deadline)
            return self._released
        finally:
            self._io.release()

    def _wait_exit(self, deadline, *, cancellable=False):
        child = self._child
        if child.returncode is not None:
            return child.returncode
        poller = None
        if self._pidfd is not None:
            try:
                poller = select.poll()
                poller.register(self._pidfd, select.POLLIN)
            except OSError:
                poller = None
        while True:
            try:
                pid, status = os.waitpid(child.pid, os.WNOHANG)
            except OSError:
                # Popen.wait()/poll() may synthesize exit 0 on ECHILD.
                return None
            if pid == child.pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                child.returncode = os.waitstatus_to_exitcode(status)
                return child.returncode
            wait = min(_POLL, deadline - monotonic())
            if wait <= 0 or (cancellable and self._cancel.is_set()):
                return None
            if poller is None:
                sleep(wait)
            else:
                try:
                    poller.poll(math.ceil(1000 * wait))
                except OSError:
                    return None

    def _cleanup(self, deadline):
        if self._released:
            return
        self._used = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        child = self._child
        if child is not None:
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            exited = self._wait_exit(min(deadline, monotonic() + _EOF_GRACE))
            if exited is None and self._pidfd is not None:
                try:
                    signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                except OSError:
                    pass
                exited = self._wait_exit(deadline)
            if exited is None:
                self._owner._fence()
                return
        _close_fd(self._pidfd)
        self._pidfd = None
        self._child = None
        self._released = True
        self._owner._release(self)
