"""Private owning-only managed Gateway, with a separate Studio opt-in."""

import re
import socket
from threading import Event, Lock, Thread, current_thread
from time import monotonic

if __package__:
    from . import (
        web_guest_duplex_exec,
        web_guest_duplex_host,
        web_guest_worker_drain,
        web_studio_guest,
        web_studio_guest_binding,
        web_studio_guest_http,
        web_studio_guest_owner,
        workspace_service,
    )
else:
    import web_guest_duplex_exec
    import web_guest_duplex_host
    import web_guest_worker_drain
    import web_studio_guest
    import web_studio_guest_binding
    import web_studio_guest_http
    import web_studio_guest_owner
    import workspace_service


_ACCEPT_SLICE = 0.2
_OBSERVE_INTERVAL = 2.0
_HEAD_LIMIT = 65536
_HEAD_SECONDS = 15
_EXEC_SECONDS = 10
_PAIR = re.compile(rb"GET /auth/connect/[A-Za-z0-9_-]{1,2048} HTTP/1\.1")


class _ManagedGateway:
    """Keep this same handle and the foreground owner/lock on uncertainty.

    It is intentionally not a context manager. Neither startup nor a drain
    verdict runs owner cleanup, history sync, or any workspace transition.
    """

    def __init__(self, owner, *, port=0, studio=False):
        if (
            type(owner) is not web_studio_guest_owner.GuestOwnerProof
            or not owner._in_original_scope()
            or type(port) is not int
            or not 0 <= port <= 65535
            or type(studio) is not bool
        ):
            raise web_studio_guest_binding.GuestBindingError()
        self._owner = owner
        self._foreground = current_thread()
        self._port = port
        self._gateway = self._listener = self._binding = self._acceptor = None
        self._studio_requested = studio
        self._studio = None
        self._retained_result = None
        self._workers = web_guest_worker_drain.AcceptedWorkers(activities_per_worker=1)
        self._relays_lock = Lock()
        self._relays = set()
        self._fenced = Event()
        self._failed = Event()
        self._start_done = Event()
        self._start_attempted = self._started = self._accept_finished = False
        self._listener_closed = False
        self._pending_accept = None

    @property
    def binding(self):
        """Return the exact live-phase handle; call current() for evidence."""
        if self._started and not self._fenced.is_set():
            return self._binding
        return None

    @property
    def url(self):
        binding = self.binding
        return None if binding is None else binding.native.binding.origin

    def start(self):
        """One explicit activation; even on False, keep this exact handle."""
        if (
            current_thread() is not self._foreground
            or not self._owner._in_original_scope()
            or self._start_attempted
        ):
            return False
        self._start_attempted = True
        started = False
        try:
            owner = self._owner
            gateway = self._gateway = workspace_service.Gateway(
                owner.owner.box, owner.pinned_data(), owner.owner.connection, self._port
            )
            proof = self._listener = gateway._reserve_listener(managed=True)
            binding = self._binding = web_studio_guest_binding._bind_managed(
                gateway, proof, owner, fenced=self._fenced, workers=self._workers
            )
            if proof.origin(gateway, listening=False) != binding.native.binding.origin:
                return False
            proof._listener.listen(32)
            proof._listener.settimeout(_ACCEPT_SLICE)
            proof._mark_listening(gateway)
            if binding.current() is not binding.native.binding:
                return False
            if self._studio_requested:
                self._studio = web_studio_guest_http._GuestStudioHttp(
                    binding, self._workers
                )
            self._acceptor = Thread(
                target=self._serve, name="oc-box-managed-gateway", daemon=True
            )
            self._acceptor.start()
            self._started = started = True
            return True
        except Exception:
            return False  # Never expose private startup diagnostics.
        finally:
            try:
                if not started:
                    self._failed.set()
                    self._fence()
                    if self._listener is not None:
                        # Even a partially started acceptor is still gated
                        # and cannot touch the listener on an ambiguous start.
                        self._close_listener()
            finally:
                self._start_done.set()

    def _fence(self):
        self._fenced.set()
        try:
            if self._studio is not None:
                self._studio.close()
        finally:
            self._workers.fence()

    def _close_listener(self):
        try:
            self._listener_closed = self._listener._close_original() is True
        except (OSError, ValueError):
            self._failed.set()
        finally:
            self._gateway.closed.set()

    def _serve(self):
        self._start_done.wait()
        if not self._started:
            return
        stream = self._listener._listener
        checked_until = 0
        clean = False
        try:
            while not self._fenced.is_set():
                if monotonic() >= checked_until:
                    if self._binding.current() is not self._binding.native.binding:
                        break
                    if self._studio is not None:
                        self._studio.expire()
                    checked_until = monotonic() + _OBSERVE_INTERVAL
                if not self._binding.listener_current():
                    return
                if self._fenced.is_set():
                    break
                try:
                    accepted, _ = stream.accept()
                except socket.timeout:
                    continue
                # This transient result is charged to the acceptor until
                # either exact rejection/close or tracker ownership transfer.
                self._pending_accept = accepted
                if not self._dispatch(accepted):
                    return
            clean = self._fenced.is_set() and self._binding.listener_current()
        except Exception:
            pass
        finally:
            if not clean:
                self._failed.set()
            try:
                self._fence()
            finally:
                self._close_listener()
                self._accept_finished = True

    def _dispatch(self, stream):
        stamp = web_guest_worker_drain._stamp(stream)
        original = self._binding.listener_current() and (
            stream.getsockname() == self._listener._address
        )
        if original and not self._fenced.is_set():
            if self._workers.dispatch(stream, self._forward):
                self._pending_accept = None
                return True
            # Only a definite False leaves ownership with the acceptor.
        if not web_guest_worker_drain._same(stream, stamp, disown=True):
            return False
        stream.close()
        if stream.fileno() >= 0:
            return False
        self._pending_accept = None
        return original

    def _head(self, use):
        data = bytearray()
        stream = use.stream
        deadline = monotonic() + _HEAD_SECONDS
        while len(data) < _HEAD_LIMIT and not self._fenced.is_set():
            left = deadline - monotonic()
            if left <= 0 or not self._workers.current(use):
                return None
            stream.settimeout(left)
            part = stream.recv(4096)
            if not part:
                return None
            data.extend(part)
            end = data.find(b"\r\n\r\n", max(0, len(data) - len(part) - 3))
            if end >= 0:
                if end + 4 <= _HEAD_LIMIT and monotonic() < deadline:
                    return bytes(data)
                return None
        return None

    def _forward(self, use):
        relay = None
        try:
            initial = self._head(use)
            if initial is None or self._fenced.is_set():
                return
            studio = self._studio
            if studio is not None and studio.handle(use, initial):
                return
            relay = web_guest_duplex_exec.GuestDuplexExec(
                self._binding, self._workers, use
            )
            with self._relays_lock:
                self._relays.add(relay)
            tunnel = relay.start(deadline=monotonic() + _EXEC_SECONDS)
            # Only a native pairing reply may redirect to the first-party
            # document. The original issue/exchange, cookie and other native
            # routes are unchanged. Non-upgrade replies close as before.
            pairing = (
                web_studio_guest.PAGE_PATH
                if studio is not None and _PAIR.fullmatch(initial.split(b"\r\n", 1)[0])
                else None
            )
            web_guest_duplex_host.pump_worker(
                self._workers, use, tunnel, initial, pairing_path=pairing
            )
        except (OSError, ValueError):
            pass
        finally:
            if relay is not None:
                retired = False
                try:
                    retired = relay.retire() is True
                finally:
                    if retired:
                        with self._relays_lock:
                            self._relays.remove(relay)
                    else:
                        self._failed.set()
                        self._fence()

    def drain(self, *, join_timeout=5.0):
        """Fence, join within one budget, then recheck the exact held lease.

        The final lease observation uses the existing bounded inspect. True
        is transport/lease evidence only; it is not permission to stop an
        application job, sync history, or retire the owner.
        """
        return (
            self.drain_review(join_timeout=join_timeout)
            is web_guest_worker_drain.DrainReview.VERIFIED
        )

    def drain_review(self, *, join_timeout=5.0):
        """Report a bounded join separately from an unverified outcome."""
        verdict = web_guest_worker_drain.DrainReview
        if (
            current_thread() is not self._foreground
            or not self._start_attempted
            or type(join_timeout) not in (int, float)
            or not 0 <= join_timeout <= 30
        ):
            return verdict.UNCONFIRMED
        deadline = monotonic() + join_timeout
        self._fence()
        if not self._started:
            return verdict.UNCONFIRMED
        self._acceptor.join(max(0.0, deadline - monotonic()))
        if self._acceptor.is_alive():
            return verdict.UNCONFIRMED if self._failed.is_set() else verdict.WAITING
        if not (
            self._accept_finished
            and self._listener_closed
            and self._gateway.listener is self._listener._listener
            and self._gateway._listener_proof is self._listener
            and self._pending_accept is None
            and not self._failed.is_set()
        ):
            return verdict.UNCONFIRMED
        workers = self._workers.close_review(timeout=max(0.0, deadline - monotonic()))
        if workers is not verdict.VERIFIED:
            return verdict.UNCONFIRMED if self._failed.is_set() else workers
        with self._relays_lock:
            relays_drained = not self._relays
        if (
            relays_drained
            and (self._studio is None or self._studio.drained())
            and not self._failed.is_set()
            and self._binding.lease_current()
        ):
            return verdict.VERIFIED
        return verdict.UNCONFIRMED


_NOT_CAPTURED = object()


class _RetainedRunResult:
    """Unserved foreground result; owning scope and exact handle must stay held.

    This never releases a lease, clears a stop record, chooses an exit code,
    syncs history, or stops anything. A future caller must arm its durable
    lifecycle fence before startup and prevent ordinary unwind on uncertainty.
    """

    def __init__(self, gateway):
        if (
            type(gateway) is not _ManagedGateway
            or gateway._foreground is not current_thread()
            or not gateway._owner._in_original_scope()
            or gateway._retained_result is not None
        ):
            raise web_studio_guest_binding.GuestBindingError()
        self._gateway = gateway
        self._raw = _NOT_CAPTURED
        self._verdict = None
        gateway._retained_result = self

    def _foreground(self):
        if (
            self._gateway._foreground is not current_thread()
            or not self._gateway._owner._in_original_scope()
        ):
            raise web_studio_guest_binding.GuestBindingError()

    def capture(self, raw):
        """Capture a raw nonzero/signal result or explicit unknown exactly once."""
        self._foreground()
        if self._raw is not _NOT_CAPTURED or (
            raw is not None and (type(raw) is not int or not -255 <= raw <= 255)
        ):
            raise web_studio_guest_binding.GuestBindingError()
        self._raw = raw

    @property
    def raw(self):
        if self._raw is _NOT_CAPTURED:
            raise web_studio_guest_binding.GuestBindingError()
        return self._raw

    def observe(self, *, join_timeout=5.0):
        """Re-observe only an explicit pending join, never an unknown outcome."""
        self._foreground()
        _ = self.raw  # The raw result is independent of every drain observation.
        verdict = web_guest_worker_drain.DrainReview
        if self._verdict in (verdict.VERIFIED, verdict.UNCONFIRMED):
            return self._verdict
        self._verdict = verdict.UNCONFIRMED
        try:
            result = self._gateway.drain_review(join_timeout=join_timeout)
        except (KeyboardInterrupt, Exception):
            return self._verdict
        if type(result) is verdict:
            self._verdict = result
        return self._verdict
