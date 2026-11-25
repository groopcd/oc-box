"""Host-only transport for one guarded, cancellable history source read.

The owner supplies a pinned command, secret-free subprocess environment, short
admission gate and independent remote verification. This module does not select
containers, verify native history, schedule work, or run lifecycle commands.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
import os
import re
import secrets
import selectors
import signal
import subprocess
from threading import current_thread
import time
from typing import Callable, Literal, Mapping, Sequence


MAX_REQUEST = 16384
MAX_RESPONSE = 32 * 1024 * 1024
ADMISSION_SECONDS = 1.0
_CHUNK = 65536
_SLICE = 0.05
_READY_PREFIX = b"OC-BOX-HISTORY-READY/1 "
_GO_PREFIX = b"OC-BOX-HISTORY-GO/1 "


@dataclass(frozen=True, eq=False, slots=True, repr=False)
class SourceAttempt:
    """Fresh, single-operation binding; the owner pins the runtime generation."""

    container_id: str
    generation: str
    token: str = field(default_factory=lambda: secrets.token_hex(16), init=False)
    _used: bool = field(default=False, init=False)

    def __post_init__(self):
        if (
            type(self.container_id) is not str
            or re.fullmatch(r"[a-f0-9]{64}", self.container_id) is None
            or type(self.generation) is not str
            or not 1 <= len(self.generation) <= 128
        ):
            raise ValueError("Invalid source attempt binding")


@dataclass(frozen=True, slots=True)
class HelperAbsent:
    """Trusted verifier's fresh absence of the exact attempt's remote helper.

    The producer must independently bind the helper to this attempt (not only
    a PID), and rule out delayed launch of the same attempt. An unregistered
    helper, failed query, output receipt, or missing wrapper is not proof.
    """

    attempt: SourceAttempt
    observed_ns: int


@dataclass(frozen=True, slots=True)
class OriginalStopped:
    """Trusted verifier's fresh physical stop of the same original resource.

    The producer independently verifies identity and physical stop while
    holding the appropriate lifetime fences; running=false alone is not proof.
    """

    attempt: SourceAttempt
    observed_ns: int


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """Local observations only; never use status/EOF as native cleanup proof."""

    status: str
    spawn_attempted: bool
    returncode: int | None
    stdout_eof: bool
    stderr_eof: bool
    local_cleanup_verified: bool
    _retired_ns: int | None = field(repr=False)
    _attempt: SourceAttempt = field(repr=False)

    def remote_cleanup(
        self, proof: HelperAbsent | OriginalStopped | None = None
    ) -> Literal["not-started", "helper-retired", "original-stopped", "unverified"]:
        """Interpret one independently obtained witness, not authority to act."""
        if not self.local_cleanup_verified:
            return "unverified"
        if not self.spawn_attempted:
            return "not-started"
        if (
            type(proof) not in (HelperAbsent, OriginalStopped)
            or proof.attempt is not self._attempt
            or type(proof.observed_ns) is not int
            or self._retired_ns is None
            or not self._retired_ns <= proof.observed_ns <= time.monotonic_ns()
        ):
            return "unverified"
        return "helper-retired" if type(proof) is HelperAbsent else "original-stopped"


class _Fault(Exception):
    """Internal, fixed category; contains no command, frame, or diagnostic."""


def _pidfd_available():
    if (
        not hasattr(os, "pidfd_open")
        or not hasattr(os, "P_PIDFD")
        or not hasattr(signal, "pidfd_send_signal")
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
    ):
        return False
    try:
        fd = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(fd, 0)
            try:
                os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
            except ChildProcessError:
                # The kernel recognizes P_PIDFD, but self is not a child.
                pass
            else:
                return False
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


class SourceRead:
    """One owned Popen and its private pipes, used on the launching thread only.

    Enter as a context manager or always call finish/cancel in the owner's
    finally path. Normal poll does at most one nonblocking I/O round. A fault
    closes stdin and settles local cleanup with its own finite budget.

    Wire: a four-byte unsigned big-endian size followed by exactly that
    many opaque bytes in each direction; empty/second frames and stderr are
    refused. Optional two-phase mode inserts READY and a separately admitted
    GO between these frames. Stdin remains open until a full result or
    cancellation; the peer must independently handle control EOF. The host
    still requires both actual EOFs and wait status after receiving the frame.
    There is no replay or remote signal.
    """

    @classmethod
    def start(
        cls,
        argv: Sequence[str],
        *,
        attempt: SourceAttempt,
        env: Mapping[str, str],
        payload: bytes,
        admission: Callable[[float], AbstractContextManager],
        deadline: float,
        max_output: int,
        cancelled: Callable[[], bool] | None = None,
        cleanup: float = 2,
        two_phase: bool = False,
        executable: str | None = None,
    ):
        """Invoke actual Popen only inside this operation's admission context.

        ``admission`` must enforce the supplied short absolute deadline in
        its own acquisition and identity/stop-intent checks. ``cancelled`` if
        supplied is a short, nonblocking owner-side check; neither callback is
        run in a thread. Do not have another component poll or reap the child.
        No credential may be included in argv or env by the caller.

        For ``two_phase=True`` the opaque request must bind the audited peer
        to the same attempt token. Use ``wait_ready()``, acquire a
        ``RemoteWitness`` for those exact bytes, then call ``go()``. A
        protocol fault never falls back to one-shot mode.
        """
        now = time.monotonic()
        if (
            type(attempt) is not SourceAttempt
            or type(payload) is not bytes
            or not 1 <= len(payload) <= MAX_REQUEST
            or type(deadline) not in (int, float)
            or not 0 < deadline <= now + 60
            or type(cleanup) not in (int, float)
            or not 0 < cleanup <= 5
            or type(max_output) is not int
            or not 1 <= max_output <= MAX_RESPONSE
            or not callable(admission)
            or (cancelled is not None and not callable(cancelled))
            or type(two_phase) is not bool
        ):
            raise ValueError("Invalid source read bounds or admission")
        if isinstance(argv, (str, bytes)):
            raise ValueError("Invalid source read command")
        command = tuple(argv)
        environment = dict(env)
        if not command or not all(type(part) is str for part in command):
            raise ValueError("Invalid source read command")
        pinned = {}
        if executable is not None:
            if (
                not two_phase
                or command[0] != "podman"
                or type(executable) is not str
                or re.fullmatch(
                    rf"/proc/{os.getpid()}/fd/[1-9][0-9]{{0,9}}", executable
                )
                is None
            ):
                raise ValueError("Invalid source engine pin")
            pinned = {"executable": executable, "cwd": "/"}
        if attempt._used:
            raise ValueError("Source attempt is single-use")
        object.__setattr__(attempt, "_used", True)

        read = cls()
        read._owner = current_thread()
        read._process = os.getpid()
        read._attempt = attempt
        read._request = len(payload).to_bytes(4, "big") + payload
        read._offset = 0
        read._deadline, read._cancelled = deadline, cancelled
        read._cleanup_budget, read._limit = cleanup, max_output
        read._child = read._pidfd = read._selector = None
        read._spawn_attempted = read._wait_lost = False
        read._returncode = None
        read._eof = set()
        read._response = bytearray()
        read._expected = None
        read._outcome = None
        read._reply = None
        read._barrier = None
        read._ready_partial = bytearray()
        read._ready_record = None
        read._go_sent = read._go_busy = False
        if two_phase:
            if __package__:
                from . import history_overlap_remote
            else:
                import history_overlap_remote
            read._barrier = history_overlap_remote
        reason = "admission-refused"
        try:
            read._check("before-start")
            if not _pidfd_available():
                raise _Fault("unsupported-supervision")
            gate_end = min(deadline, time.monotonic() + ADMISSION_SECONDS)
            with admission(gate_end):
                read._check("before-start")
                if time.monotonic() >= gate_end:
                    raise _Fault("admission-timeout")
                if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
                    raise _Fault("unsupported-supervision")
                read._spawn_attempted = True
                read._child = subprocess.Popen(
                    command,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    close_fds=True,
                    start_new_session=True,
                    **pinned,
                )
            # Even a context suppressing an exception cannot fabricate a child.
            if read._child is None:
                raise _Fault("spawn-refused")
            if time.monotonic() >= gate_end:
                raise _Fault("admission-timeout")
            read._check()
            reason = "supervision-refused"
            read._setup()
        except BaseException as error:
            if isinstance(error, _Fault):
                reason = error.args[0]
            elif read._spawn_attempted and read._child is None:
                reason = "spawn-refused"
            read._abort(reason)
            if not isinstance(error, Exception):
                error.source_outcome = read._outcome
                raise
        if read._outcome is None:
            # Start delivering immediately; small requests can overlap even
            # while the owner is in a synchronous destination operation.
            read.poll()
        return read

    def _thread(self):
        if current_thread() is not self._owner or os.getpid() != self._process:
            raise RuntimeError("Source read is owned by its launching thread")
        if self._go_busy:
            raise RuntimeError("Source read cannot reenter its GO gate")

    def __enter__(self):
        self._thread()
        return self

    def __exit__(self, _kind, error, _traceback):
        # A completed read must not retain an unread transcript past scope.
        outcome = self.cancel()
        if error is not None:
            error.source_outcome = outcome

    def _check(self, when=None):
        if self._cancelled is not None and self._cancelled():
            raise _Fault("cancelled-before-start" if when else "cancelled")
        if time.monotonic() >= self._deadline:
            raise _Fault("timeout-before-start" if when else "timeout")

    def _setup(self, *, retiring=False):
        if self._selector is None:
            self._selector = selectors.DefaultSelector()
        streams = ("stdout", "stderr") if retiring else ("stdout", "stderr", "stdin")
        for name in streams:
            stream = getattr(self._child, name)
            if stream.closed or name in self._eof or stream in self._selector.get_map():
                continue
            os.set_blocking(stream.fileno(), False)
            events = selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ
            self._selector.register(stream, events, name)
        if self._pidfd is None:
            self._pidfd = os.pidfd_open(self._child.pid)

    def _status(self):
        # Popen.poll()/wait() can manufacture an exit 0 on ECHILD. Only record
        # the kernel's actual terminal status for this original unreaped child.
        if self._returncode is not None or self._wait_lost:
            return
        code = None
        try:
            if self._pidfd is None:
                # A failed pin may still permit reaping our own child. Never
                # signal on a numeric PID, including in this fallback.
                pid, status = os.waitpid(self._child.pid, os.WNOHANG)
                if pid == self._child.pid and (
                    os.WIFEXITED(status) or os.WIFSIGNALED(status)
                ):
                    code = os.waitstatus_to_exitcode(status)
            else:
                status = os.waitid(os.P_PIDFD, self._pidfd, os.WEXITED | os.WNOHANG)
                if status is not None:
                    if status.si_pid != self._child.pid:
                        self._wait_lost = True
                    elif status.si_code == os.CLD_EXITED:
                        code = status.si_status
                    elif status.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                        code = -status.si_status
                    else:
                        self._wait_lost = True
        except ChildProcessError:
            self._wait_lost = True
        if code is not None:
            self._returncode = self._child.returncode = code

    def _unregister(self, stream):
        if self._selector is not None and stream in self._selector.get_map():
            self._selector.unregister(stream)

    def _close_input(self):
        self._request = None
        if self._child is not None:
            stream = self._child.stdin
            if not stream.closed:
                try:
                    self._unregister(stream)
                finally:
                    stream.close()

    def _send(self, key):
        end = self._offset + _CHUNK
        sent = os.write(key.fd, self._request[self._offset : end])
        if sent <= 0:
            raise _Fault("request-write")
        self._offset += sent
        if self._offset == len(self._request):
            self._request = None
            self._unregister(key.fileobj)

    def _receive(self, key, *, retiring):
        pre_go = not retiring and self._barrier is not None and not self._go_sent
        size = _CHUNK
        if not retiring and key.data == "stdout":
            if pre_go:
                size = (
                    1
                    if self._ready_record is not None
                    else self._barrier.MAX_READY_BYTES + 1 - len(self._ready_partial)
                )
            else:
                expected = 4 if self._expected is None else self._expected
                size = min(_CHUNK, expected + 1 - len(self._response))
        chunk = os.read(key.fd, size)
        if not chunk:
            self._eof.add(key.data)
            self._unregister(key.fileobj)
            if pre_go:
                raise _Fault("ready-eof")
        elif not retiring:
            if key.data == "stderr":
                raise _Fault("unexpected-stderr")
            if pre_go:
                self._receive_ready(chunk)
                return
            self._response.extend(chunk)
            if self._expected is None and len(self._response) >= 4:
                size = int.from_bytes(self._response[:4], "big")
                if not 1 <= size <= self._limit:
                    raise _Fault("response-limit")
                self._expected = size + 4
            if self._expected is not None and len(self._response) >= self._expected:
                if len(self._response) != self._expected or self._request is not None:
                    raise _Fault("response-framing")
                self._close_input()

    def _receive_ready(self, chunk):
        if self._request is not None or self._ready_record is not None:
            raise _Fault("ready-framing")
        self._ready_partial.extend(chunk)
        prefix = _READY_PREFIX + self._attempt.token.encode("ascii") + b" "
        head = self._ready_partial[: len(prefix)]
        if len(
            self._ready_partial
        ) > self._barrier.MAX_READY_BYTES or not prefix.startswith(head):
            raise _Fault("ready-framing")
        if b"\n" in self._ready_partial:
            record = bytes(self._ready_partial)
            try:
                self._barrier._ready(self._attempt, record)
            except self._barrier.RemoteWitnessError:
                raise _Fault("ready-framing") from None
            self._ready_record = record
            self._ready_partial.clear()

    def _step(self, wait, *, retiring=False):
        if self._selector is None:
            time.sleep(wait)
            return
        events = self._selector.select(wait)
        if not retiring and self._barrier is not None and not self._go_sent:
            # Do not let a writable stdin mask already queued premature READY.
            events.sort(key=lambda event: event[0].data == "stdin")
        for key, _ in events:
            try:
                if key.data == "stdin":
                    if not retiring:
                        self._check()
                        self._send(key)
                else:
                    self._receive(key, retiring=retiring)
            except BlockingIOError:
                continue

    def _close(self):
        ok = True
        if self._child is not None:
            for stream in (self._child.stdin, self._child.stdout, self._child.stderr):
                try:
                    stream.close()
                except OSError:
                    ok = False
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        if self._pidfd is not None:
            try:
                os.close(self._pidfd)
            except OSError:
                ok = False
            self._pidfd = None
        return ok

    def _settled(self):
        return self._returncode is not None and self._eof == {"stdout", "stderr"}

    def _record(self, reason, clean):
        self._outcome = SourceOutcome(
            reason,
            self._spawn_attempted,
            self._returncode,
            "stdout" in self._eof,
            "stderr" in self._eof,
            clean,
            time.monotonic_ns() if clean else None,
            self._attempt,
        )
        return self._outcome

    def _abort(self, reason):
        self._reply = None
        self._response.clear()
        self._ready_record = None
        self._ready_partial.clear()
        if self._child is None:
            self._close_input()
            # A Popen call interrupted before it returned an owned handle is
            # not evidence no local or remote operation began.
            return self._record(reason, not self._spawn_attempted)
        end = time.monotonic() + self._cleanup_budget
        force_at = end - self._cleanup_budget / 2
        clean = False
        forced = False
        try:
            self._close_input()
            try:
                self._setup(retiring=True)
            except (OSError, ValueError):
                # Still make a bounded attempt to reap even if pipe setup or
                # pidfd acquisition refused. Never signal without the pidfd.
                pass
            while time.monotonic() < end:
                self._status()
                if self._settled():
                    clean = True
                    break
                if self._wait_lost and self._eof == {"stdout", "stderr"}:
                    break
                if not forced and time.monotonic() >= force_at:
                    forced = True
                    if (
                        self._pidfd is not None
                        and not self._wait_lost
                        and self._returncode is None
                    ):
                        try:
                            # Do not signal a process group, numeric PID, or
                            # remote helper. SIGKILL cannot be caught/forwarded.
                            signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                        except OSError:
                            pass
                remaining = (end if forced else force_at) - time.monotonic()
                self._step(max(0, min(_SLICE, remaining)), retiring=True)
        except (OSError, ValueError):
            clean = False
        finally:
            closed = self._close()
            self._record(reason, clean and closed)
        return self._outcome

    def _sample(self, wait):
        self._check()
        self._step(min(wait, max(0, self._deadline - time.monotonic())))
        self._check()
        self._status()
        if self._wait_lost:
            raise _Fault("wrapper-status-unverified")
        if self._returncode not in (None, 0):
            raise _Fault("wrapper-exit")
        if (
            self._barrier is not None
            and not self._go_sent
            and (self._eof or self._returncode is not None)
        ):
            raise _Fault("ready-eof")

    def _advance(self, wait):
        try:
            self._sample(wait)
            if "stdout" in self._eof and len(self._response) != self._expected:
                raise _Fault("response-framing")
            if self._settled():
                if self._expected is None or self._request is not None:
                    raise _Fault("response-framing")
                self._reply = bytes(memoryview(self._response)[4:])
                self._response.clear()
                self._check()
                closed = self._close()
                if not closed:
                    self._reply = None
                    return self._record("pipe-close", False)
                if time.monotonic() >= self._deadline:
                    self._reply = None
                    return self._record("timeout", True)
                return self._record("local-complete", True)
        except BaseException as error:
            self._abort(error.args[0] if isinstance(error, _Fault) else "source-io")
            if not isinstance(error, Exception):
                error.source_outcome = self._outcome
                raise
            return self._outcome
        return None

    def poll(self):
        """None while pending; otherwise a fixed-category local outcome."""
        self._thread()
        return self._outcome if self._outcome is not None else self._advance(0)

    def wait_ready(self) -> bytes | None:
        """Return the exact READY record or None on a terminal pre-GO fault.

        This neither acquires a witness nor sends GO. On None, ``finish()``
        returns the local outcome. Use the same attempt and these bytes for
        ``RemoteWitness.acquire``; keep that witness for cleanup separately.
        """
        self._thread()
        if self._barrier is None or self._go_sent:
            raise RuntimeError("Source read has no pending READY barrier")
        while self._outcome is None and self._ready_record is None:
            self._advance(_SLICE)
        if self._outcome is None:
            self._advance(0)  # Reject already queued trailing output or EOF.
        return self._ready_record

    def _matched_witness(self, witness):
        if (
            type(witness) is not self._barrier.RemoteWitness
            or witness._attempt is not self._attempt
        ):
            return False
        # RemoteWitness pins canonical in-container IDs and start ticks for
        # the controller and child after its independent top/proc readback.
        binding = tuple((info[1][-1], info[0][1]) for _, info in witness._bindings[1:])
        return binding == self._barrier._ready(self._attempt, self._ready_record)

    def go(
        self,
        witness,
        *,
        admission: Callable[[float], AbstractContextManager],
    ) -> SourceOutcome | None:
        """Write one whole GO only inside a fresh, short admission context.

        None means GO was written and the gate exited successfully. A
        returned outcome means the attempt is closed, including if gate
        release failed after bytes were sent. This never establishes remote
        cleanup. The owner still verifies it independently after retirement.

        Pass the exact ``RemoteWitness`` acquired for this read's READY bytes.
        Output already observed before GO is refused. The two pipe directions
        cannot atomically establish when concurrently arriving output was
        sent; the audited peer must actually wait for the GO record.
        """
        self._thread()
        if self._barrier is None:
            raise RuntimeError("Source read has no READY barrier")
        if self._outcome is not None:
            if self._outcome.status == "local-complete":
                self._reply = None
                self._outcome = replace(self._outcome, status="go-unexpected")
            return self._outcome
        if self._go_sent or self._ready_record is None:
            return self._abort("go-unexpected")
        self._go_busy = True
        reason = "go-witness-refused"
        try:
            if not self._matched_witness(witness):
                raise _Fault(reason)
            self._sample(0)  # Before potentially waiting to acquire the gate.
            end = min(self._deadline, time.monotonic() + ADMISSION_SECONDS)
            record = _GO_PREFIX + self._attempt.token.encode("ascii") + b"\n"
            sent = False
            reason = "go-admission-refused"
            with admission(end):
                self._sample(0)
                if time.monotonic() >= end:
                    raise _Fault("go-admission-timeout")
                reason = "go-witness-refused"
                witness.check_ready(deadline=end)
                self._sample(0)  # Detect input queued during the proc recheck.
                if time.monotonic() >= end:
                    raise _Fault("go-admission-timeout")
                self._check()  # Stop can be published while the witness is being checked.
                reason = "go-write"
                # This fixed record is below POSIX PIPE_BUF, and the pipe
                # is nonblocking. Refuse short writes or backpressure; no retry.
                if os.write(self._child.stdin.fileno(), record) != len(record):
                    raise _Fault("go-write")
                sent = True
                reason = "go-admission-refused"
            if not sent:  # A context manager may suppress an in-gate error.
                raise _Fault(reason)
            if time.monotonic() >= end:
                raise _Fault("go-admission-timeout")
            self._check()
            self._go_sent = True
            self._ready_record = None
        except BaseException as error:
            outcome = self._abort(
                error.args[0] if isinstance(error, _Fault) else reason
            )
            if not isinstance(error, Exception):
                error.source_outcome = outcome
                raise
            return outcome
        finally:
            self._go_busy = False
        return None

    def finish(self):
        """Drive to a local outcome; a two-phase read without GO is cancelled."""
        self._thread()
        if self._outcome is None and self._barrier is not None and not self._go_sent:
            return self._abort("go-required")
        while self._outcome is None:
            self._advance(_SLICE)
        return self._outcome

    def cancel(self):
        """Close the control pipe, revoke any unread reply, and retire locally."""
        self._thread()
        if self._outcome is None:
            return self._abort("cancelled")
        self._reply = None
        return self._outcome

    def response(self, proof: HelperAbsent):
        """Take one reply only after independent exact-helper absence.

        The launcher still checks stop intent at each destination operation.
        Physical stop settles cleanup but never admits a response.
        """
        self._thread()
        outcome = self._outcome
        if (
            outcome is None
            or outcome.status != "local-complete"
            or outcome.remote_cleanup(proof) != "helper-retired"
        ):
            return None
        if self._cancelled is not None:
            try:
                if self._cancelled():
                    self._reply = None
            except BaseException as error:
                self._reply = None
                if not isinstance(error, Exception):
                    error.source_outcome = outcome
                    raise
        reply, self._reply = self._reply, None
        return reply
