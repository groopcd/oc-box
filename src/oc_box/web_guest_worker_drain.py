"""Unserved accounting for accepted browser workers and their exact helpers."""

from dataclasses import dataclass, field
import errno
import os
import socket
import stat
from threading import Event, Lock, Thread, current_thread
from time import monotonic


class WorkerDrainError(ValueError):
    def __init__(self):
        super().__init__("The browser worker lifetime could not be verified.")


def _stamp(stream):
    fd = stream.fileno()
    info = os.fstat(fd)
    if not stat.S_ISSOCK(info.st_mode):
        raise WorkerDrainError()
    return fd, info.st_dev, info.st_ino


def _same(stream, stamp, *, disown=False):
    """Optionally disown a confirmed mismatch for an already-owned wrapper."""
    fd = stream.fileno()
    try:
        info = os.fstat(fd)
    except OSError as error:
        if not disown or fd < 0 or error.errno != errno.EBADF:
            return False
        # An OS-closed descriptor can be reused before the wrapper is freed.
    else:
        if stat.S_ISSOCK(info.st_mode) and (fd, info.st_dev, info.st_ino) == stamp:
            return stream.fileno() == fd
    if disown and stream.fileno() == fd:
        # Once registered, this wrapper must not keep finalizer ownership of
        # a known foreign or OS-closed fd. Detach without closing that number.
        stream.detach()
    return False


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class AcceptedWorker:
    """Exact borrowed stream; only its registry may close it."""

    stream: socket.socket
    _cancelled: Event = field(default_factory=Event, init=False)

    @property
    def cancelled(self):
        return self._cancelled.is_set()


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class WorkerActivity:
    """One registered relay or helper, not evidence that it started or ended."""


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class _Notification:
    wake: object


@dataclass(slots=True, repr=False)
class _Activity:
    notice: _Notification
    uncertain: bool = False


@dataclass(slots=True, repr=False)
class _Worker:
    stamp: tuple
    thread: Thread
    start_done: Event = field(default_factory=Event)
    activities: dict[WorkerActivity, _Activity] = field(default_factory=dict)
    ended: bool = False
    closed: bool = False


class AcceptedWorkers:
    """One private worker lifetime; no listener, HTTP, native I/O or child owner.

    Registration transfers the socket, even if thread startup raises. A
    False return does not. Fence the listener and join the actual acceptor
    separately; this registry has no view of either.
    The adapter must pass the original, uniquely owned socket from its
    verified listener. This is not listener/owner or peer-origin proof.
    """

    def __init__(self, *, capacity=32, activities_per_worker=8):
        if (
            type(capacity) is not int
            or not 1 <= capacity <= 128
            or type(activities_per_worker) is not int
            or not 1 <= activities_per_worker <= 32
        ):
            raise WorkerDrainError()
        self._capacity = capacity
        self._activity_limit = activities_per_worker
        self._lock = Lock()
        self._workers: dict[AcceptedWorker, _Worker] = {}
        self._notifications: set[_Notification] = set()
        self._closed = False
        self._failed = False

    def dispatch(self, stream, run):
        """Register the original connected stream and worker, then start once.

        The callback runs as ``run(AcceptedWorker)``. Do not close, detach,
        duplicate, transfer, or retain the borrowed stream beyond the worker.
        """
        if type(stream) is not socket.socket or not callable(run):
            return False
        try:
            stamp = _stamp(stream)
            if (
                stream.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                != socket.SOCK_STREAM
                or stream.get_inheritable()
                or not _same(stream, stamp)
            ):
                return False
            stream.getpeername()
        except (OSError, ValueError):
            return False
        use = AcceptedWorker(stream)
        thread = Thread(
            target=self._run, args=(use, run), name="oc-box-browser-worker", daemon=True
        )
        with self._lock:
            self._reap()
            if (
                self._closed
                or len(self._workers) >= self._capacity
                or not _same(stream, stamp)
                # A duplicated descriptor is not a different worker socket.
                or any(row.stamp[1:] == stamp[1:] for row in self._workers.values())
            ):
                return False
            row = self._workers[use] = _Worker(stamp, thread)
        pending = []
        try:
            thread.start()
        except BaseException as error:
            # Thread.start may have crossed the OS boundary. Never dispatch
            # it or let a later exit launder ambiguous start.
            with self._lock:
                self._failed = True
                self._fence(pending)
            if not isinstance(error, Exception):
                raise
            raise WorkerDrainError() from None
        finally:
            row.start_done.set()
            self._notify(pending)
        return True

    def _run(self, use, run):
        failed = False
        try:
            # No dispatch until the registering thread gets a definite start
            # result. Close can fence while start itself is still in flight.
            with self._lock:
                row = self._workers[use]
            row.start_done.wait()
            if self.current(use):
                run(use)
        except Exception:
            pass  # A request failure is not itself a cleanup failure.
        except BaseException:
            failed = True
        finally:
            pending = []
            try:
                with self._lock:
                    row = self._workers[use]
                    try:
                        if _same(use.stream, row.stamp, disown=True):
                            use.stream.close()
                            row.closed = use.stream.fileno() < 0
                    except (OSError, ValueError):
                        pass
                    row.ended = True
                    if failed or not row.closed or row.activities:
                        self._failed = True
                        self._fence(pending)
            finally:
                self._notify(pending)

    def _live(self, use):
        if self._closed or type(use) is not AcceptedWorker or use.cancelled:
            return None
        row = self._workers.get(use)
        if row is not None and not row.ended and row.thread is current_thread():
            return row
        return None

    def current(self, use):
        """Validate the worker's exact stream and fence before dispatch or I/O."""
        pending = []
        try:
            with self._lock:
                row = self._live(use)
                if row is not None:
                    if _same(use.stream, row.stamp, disown=True):
                        return True
                    self._failed = True
                    self._fence(pending)
                return False
        finally:
            self._notify(pending)

    def admit(self, use, *, wake):
        """Reserve one exact activity before it may launch or do native work.

        ``wake`` is a nonblocking, idempotent, non-retaining trusted callback.
        It must honor cancellation before and during startup, even if called
        after the work has retired. Check the fence again before launch.
        """
        if not callable(wake):
            raise WorkerDrainError()
        pending = []
        try:
            with self._lock:
                row = self._live(use)
                if row is None:
                    return None
                if not _same(use.stream, row.stamp, disown=True):
                    self._failed = True
                    self._fence(pending)
                    return None
                if len(row.activities) >= self._activity_limit:
                    return None
                activity = WorkerActivity()
                row.activities[activity] = _Activity(_Notification(wake))
                return activity
        finally:
            self._notify(pending)

    def complete(self, use, activity, *, confirmed):
        """Accept only explicit exact-activity retirement from its worker.

        True is independent evidence supplied by the trusted transport: no
        launch occurred, or the exact native work ended and retired cleanly.
        False/unknown is sticky. A local forced reap is not positive proof.
        Call only after the entire activity ended, not from its wake callback.
        """
        if type(use) is not AcceptedWorker or type(activity) is not WorkerActivity:
            return False
        pending = []
        try:
            with self._lock:
                row = self._workers.get(use)
                if (
                    row is None
                    or row.ended
                    or row.thread is not current_thread()
                    or activity not in row.activities
                ):
                    return False
                entry = row.activities[activity]
                same = _same(use.stream, row.stamp, disown=True)
                if same and entry.uncertain:
                    return False
                if same and confirmed is True:
                    del row.activities[activity]
                    return True
                entry.uncertain = True
                self._failed = True
                self._fence(pending)
                return False
        finally:
            self._notify(pending)

    def _fence(self, pending):
        # Caller holds the lock. Only the original accepted descriptor may
        # be woken; its owning worker remains responsible for closing it.
        self._closed = True
        for use, row in self._workers.items():
            same = row.closed or _same(use.stream, row.stamp, disown=True)
            if not same:
                self._failed = True
            if use.cancelled:
                continue
            use._cancelled.set()
            if same and not row.closed:
                try:
                    use.stream.shutdown(socket.SHUT_RDWR)
                except OSError as error:
                    if error.errno != errno.ENOTCONN:
                        self._failed = True
                    _same(use.stream, row.stamp, disown=True)
            for entry in row.activities.values():
                notice = entry.notice
                self._notifications.add(notice)
                pending.append(notice)

    def _notify(self, pending):
        # The trusted hooks may only latch or wake exact registered work.
        # Never call arbitrary cleanup, joins or transport waits here.
        interruption = None
        while pending:
            notice = pending.pop()
            failed = True
            try:
                notice.wake()
                failed = False
            except BaseException as error:
                if not isinstance(error, Exception) and interruption is None:
                    interruption = error
            finally:
                with self._lock:
                    self._notifications.remove(notice)
                    self._failed |= failed
        if interruption is not None:
            raise interruption

    def fence(self):
        """Stop admission and deliver wake signals, not a shutdown verdict."""
        pending = []
        try:
            with self._lock:
                self._fence(pending)
        finally:
            self._notify(pending)

    def _reap(self):
        # Only discard exact records after the target returned and the real
        # thread terminated; len(children)==0 is never a substitute for this.
        if self._failed:
            return
        for use, row in tuple(self._workers.items()):
            if (
                row.ended
                and row.closed
                and not row.activities
                and not row.thread.is_alive()
            ):
                row.thread.join(0)
                del self._workers[use]

    def close(self, *, timeout=5.0):
        """Fence and bounded-join; a timeout can recheck only this same drain."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise WorkerDrainError()
        deadline = monotonic() + timeout
        self.fence()
        with self._lock:
            workers = tuple(self._workers.values())
        for row in workers:
            worker = row.thread
            if worker is current_thread():
                return False
            if not row.start_done.wait(max(0.0, deadline - monotonic())):
                return False
            if worker.ident is not None:
                try:
                    worker.join(max(0.0, deadline - monotonic()))
                except RuntimeError:  # A start exception can leave a partial thread.
                    with self._lock:
                        self._failed = True
                    return False
        with self._lock:
            self._reap()
            return not self._failed and not self._workers and not self._notifications
