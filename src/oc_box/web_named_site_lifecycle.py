"""Internal, one-shot lifecycle for the two reserved named HTTPS sites.

Nothing calls this module from the launcher. Route selection within each site
belongs to trusted dispatch code; this owns only the two listeners, one TLS
connection per worker, and the admission/drain fence around them.
"""

import errno
import math
import os
import socket
import ssl
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Thread, current_thread
from time import monotonic

if __package__:
    from . import (
        web_local_origins,
        web_named_headers,
        web_named_listeners,
        web_named_services,
    )
else:
    import web_local_origins
    import web_named_headers
    import web_named_listeners
    import web_named_services


_WORKER_LIMIT = 8  # Both sites combined; includes incomplete TLS handshakes.
_BACKLOG = 8
_POLL_INTERVAL = 0.05
_SWEEP_INTERVAL = 1.0
_CONNECTION_SECONDS = 5.0
_DISCONNECTED = frozenset((errno.ECONNABORTED, errno.ECONNRESET, errno.ENOTCONN))


class ListenerError(ValueError):
    def __init__(self):
        super().__init__("Named HTTPS listeners could not be activated.")


class StopReason(Enum):
    REQUESTED = "requested"
    OWNER_OR_ACCOUNT = "owner-or-account"
    EXPIRED = "expired"
    WORKER_LIMIT = "worker-limit"
    FAILED = "failed"


def _identity(stream):
    fd = stream.fileno()
    stat = os.fstat(fd)
    return fd, stat.st_dev, stat.st_ino


def _has_identity(stream, identity):
    try:
        return _identity(stream) == identity
    except (OSError, ValueError):
        return False


@dataclass(frozen=True, slots=True, repr=False)
class _Site:
    listener: socket.socket
    identity: tuple[int, int, int]
    address: tuple[str, int]
    context: ssl.SSLContext

    @classmethod
    def capture(cls, listener, address, context):
        if (
            type(listener) is not socket.socket
            or type(context) is not ssl.SSLContext
            or context.protocol != ssl.PROTOCOL_TLS_SERVER
            or context.sslsocket_class is not ssl.SSLSocket
            or not callable(context.sni_callback)
        ):
            raise ListenerError()
        site = cls(listener, _identity(listener), address, context)
        site.check_unserved()
        return site

    def check_unserved(self):
        peer = self.listener
        if (
            not _has_identity(peer, self.identity)
            or peer.family != socket.AF_INET
            or peer.type != socket.SOCK_STREAM
            or peer.getsockname() != self.address
            or peer.get_inheritable()
            or peer.getsockopt(socket.SOL_SOCKET, socket.SO_PROTOCOL)
            != socket.IPPROTO_TCP
            or peer.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            or peer.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
            or peer.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT)
        ):
            raise ListenerError()
        try:
            peer.getpeername()
        except OSError as error:
            if error.errno == errno.ENOTCONN:
                return
        raise ListenerError()


def prepare_sites(
    box, *, profile, lease, store, account_current, verifier_fd, expected_sha256
):
    """Prepare services against these exact sockets, without listening.

    The trusted foreground caller keeps the original reservation and verified
    owner lease contexts open through a confirmed ``close()``. Neither socket
    may be closed, replaced or repurposed by the reservation holder in the
    interim; their TLS contexts are private to the lifecycle. There is no
    address, TLS material or owner discovery here.
    """
    try:
        if (
            not web_named_listeners._host_allowed()
            or type(profile) is not web_named_listeners.ReservedProfile
            or type(profile.origins) is not web_local_origins.LocalOrigins
            or profile.origins != web_local_origins.LocalOrigins()
            or type(profile.tls) is not web_local_origins.LocalTLS
            or profile.tls.account is profile.tls.workspace
        ):
            raise ListenerError()
        account = _Site.capture(
            profile.account, profile.origins.account_bind, profile.tls.account
        )
        workspace = _Site.capture(
            profile.workspace, profile.origins.workspace_bind, profile.tls.workspace
        )
    except (OSError, ValueError):
        raise ListenerError() from None
    services = web_named_services.prepare_services(
        box,
        profile=profile,
        lease=lease,
        store=store,
        account_current=account_current,
        verifier_fd=verifier_fd,
        expected_sha256=expected_sha256,
    )
    return _NamedSites(account, workspace, services)


@dataclass(slots=True, eq=False, repr=False)
class _Worker:
    stream: ssl.SSLSocket
    identity: tuple[int, int, int]
    deadline: float
    thread: Thread | None = None
    closed: bool = False
    interrupted: bool = False


class _NamedSites:
    def __init__(self, account, workspace, services):
        self._sites = (account, workspace)
        self._services = services
        self._lock = Lock()  # Own accept/listener close and worker socket cleanup.
        self._lifecycle = Lock()  # Serialize start/close; never used by workers.
        self._stopped = Event()
        self._reason = None
        self._borrowed = False
        self._clean = True
        self._monitor = None
        self._deadline = None
        self._workers = set()

    @property
    def services(self):
        return self._services

    @property
    def stop_reason(self):
        """Fixed lifetime result, not a drain verdict or a request diagnostic."""
        with self._lock:
            return self._reason

    def wait_stopped(self, timeout=None):
        """Wait for the accept fence; never use this as a lease-release signal."""
        return self._stopped.wait(timeout)

    def start(self, *, account, workspace, expires_at):
        """Listen once and dispatch by listener, never by client-selected data.

        Each trusted callback borrows an already handshaken TLS socket for
        one synchronous exchange: ``dispatch(stream, *, check, deadline)``.
        Frame, authorize and bound its I/O to the deadline; call ``check()``
        before admitting work, during long operations and before sending
        replies. Never close, detach, unwrap, duplicate, retain or transfer
        the stream, or manage the lifecycle from a callback. Request refusals
        can raise ``NamedRequestError``. ``expires_at`` is a trusted absolute
        monotonic lifetime deadline, not a browser-session expiry.
        """
        with self._lifecycle:
            if self._monitor is not None or self._stopped.is_set():
                raise ListenerError()
            try:
                if (
                    not callable(account)
                    or not callable(workspace)
                    or type(expires_at) not in (int, float)
                    or not math.isfinite(expires_at)
                ):
                    raise ListenerError()
                self._deadline = expires_at
                self._sweep()
                with self._lock:
                    self._check_locked()
                    for site in self._sites:
                        site.check_unserved()
                    self._borrowed = True
                    for site in self._sites:
                        context = site.context
                        context.set_alpn_protocols(["http/1.1"])
                        context.options |= ssl.OP_NO_TICKET | ssl.OP_NO_RENEGOTIATION
                        context.num_tickets = 0
                        site.listener.setblocking(False)
                        site.listener.listen(_BACKLOG)
                    self._monitor = Thread(
                        target=self._serve,
                        args=((account, workspace),),
                        name="oc-box-named-accept",
                        daemon=False,
                    )
                    self._start_thread_locked(self._monitor)
            except BaseException as error:
                self._fence(StopReason.FAILED)
                if not isinstance(error, Exception):
                    raise
                raise ListenerError() from None

    def _start_thread_locked(self, thread):
        try:
            thread.start()
        except BaseException:
            # A failed start may have raced native thread creation. Retain the
            # lifecycle fence rather than speculate about confirmed cleanup.
            self._clean = False
            raise

    def _current(self):
        services = self._services
        owner = services._owner
        sign_in = services.application.sign_in
        try:
            if (
                not sign_in._stopped.is_set()
                and not services._flow.cleanup_failed
                and not sign_in.admission.cancellation_failed
                and owner._owner_current(owner.binding) is True
            ):
                return
        except Exception:  # noqa: BLE001 - never disclose check diagnostics.
            pass
        self._fence(StopReason.OWNER_OR_ACCOUNT)
        raise web_named_headers.NamedRequestError()

    def _check_locked(self, deadline=None):
        if self._reason is None and monotonic() >= self._deadline:
            self._fence_locked(StopReason.EXPIRED)
        if self._reason is not None or (
            deadline is not None and monotonic() >= deadline
        ):
            raise web_named_headers.NamedRequestError()

    def _check(self, deadline=None):
        with self._lock:
            self._check_locked(deadline)
        self._current()
        with self._lock:
            self._check_locked(deadline)

    def _sweep(self):
        self._check()
        # Idle expiry has to run even when no browser makes another request.
        services = self._services
        services._bootstrap.expire()
        sign_in = services.application.sign_in
        sign_in.expire()
        if services._flow.cleanup_failed or sign_in.admission.cancellation_failed:
            self._fence(StopReason.FAILED)
            raise web_named_headers.NamedRequestError()

    def _accept_locked(self, site, dispatch):
        if not _has_identity(site.listener, site.identity):
            raise ListenerError()
        try:
            peer, address = site.listener.accept()
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno in _DISCONNECTED:
                return
            raise
        with peer:
            accepted = monotonic()
            self._check_locked()
            try:
                if (
                    peer.family != socket.AF_INET
                    or peer.type != socket.SOCK_STREAM
                    or peer.getsockname() != site.address
                    or peer.getpeername() != address
                    or address[0] != web_local_origins.LOOPBACK
                    or peer.get_inheritable()
                ):
                    return
                if len(self._workers) >= _WORKER_LIMIT:
                    # No queue/retry, and record saturation as a terminal result.
                    self._fence_locked(StopReason.WORKER_LIMIT)
                    return
                identity = _identity(peer)
                # Wrap before registration; there is no handshake or user I/O
                # here. After detachment only this TLS object owns the FD.
                stream = site.context.wrap_socket(
                    peer,
                    server_side=True,
                    do_handshake_on_connect=False,
                    suppress_ragged_eofs=False,
                )
            except OSError as error:
                if error.errno in _DISCONNECTED:
                    return
                raise
            worker = _Worker(
                stream, identity, min(self._deadline, accepted + _CONNECTION_SECONDS)
            )
            self._workers.add(worker)
            try:
                if type(stream) is not ssl.SSLSocket or not _has_identity(
                    stream, identity
                ):
                    raise ListenerError()
                worker.thread = Thread(
                    target=self._exchange,
                    args=(worker, dispatch),
                    name="oc-box-named-peer",
                    daemon=False,
                )
                self._start_thread_locked(worker.thread)
            except BaseException:
                self._finish_locked(worker)
                raise

    def _exchange(self, worker, dispatch):
        def check():
            self._check(worker.deadline)

        try:
            stream = worker.stream
            check()
            remaining = worker.deadline - monotonic()
            if remaining <= 0:
                raise web_named_headers.NamedRequestError()
            stream.settimeout(remaining)
            stream.do_handshake()
            if stream.session_reused or stream.selected_alpn_protocol() not in (
                None,
                "http/1.1",
            ):
                return
            check()
            dispatch(stream, check=check, deadline=worker.deadline)
            check()
        except (OSError, web_named_headers.NamedRequestError):
            pass  # An idle client or request refusal is not an owner refusal.
        except BaseException:
            self._fence(StopReason.FAILED)
        finally:
            with self._lock:
                self._finish_locked(worker)

    def _serve(self, dispatchers):
        try:
            next_sweep = monotonic() + _SWEEP_INTERVAL  # start already checked.
            while not self._stopped.is_set():
                if monotonic() >= next_sweep:
                    self._sweep()
                    next_sweep = monotonic() + _SWEEP_INTERVAL
                with self._lock:
                    self._check_locked()
                    for worker in tuple(self._workers):
                        if not worker.thread.is_alive():
                            worker.thread.join(0)
                            if worker.closed:
                                self._workers.remove(worker)
                            else:
                                # Keep an uncertain handle pinned; forgetting
                                # it could close a descriptor through GC.
                                self._clean = False
                        elif monotonic() >= worker.deadline:
                            self._cancel_locked(worker)
                    if not self._clean:
                        break
                    for site, dispatch in zip(self._sites, dispatchers):
                        if self._reason is not None:
                            break
                        self._accept_locked(site, dispatch)
                self._stopped.wait(_POLL_INTERVAL)
        except BaseException:
            pass  # Fixed lifetime verdict only; never let Thread print raw errors.
        finally:
            self._fence(StopReason.FAILED)

    def _close_locked(self, stream, identity):
        if not _has_identity(stream, identity):
            return False  # Never close a replaced descriptor.
        try:
            stream.close()
        except OSError:
            return False
        return stream.fileno() < 0

    def _finish_locked(self, worker):
        if not worker.closed:
            worker.closed = self._close_locked(worker.stream, worker.identity)
            self._clean &= worker.closed

    def _cancel_locked(self, worker):
        if worker.closed or worker.interrupted:
            return
        worker.interrupted = True
        if not _has_identity(worker.stream, worker.identity):
            self._clean = False
            return
        try:
            # Wake the TCP I/O, without clearing SSLSocket's TLS state or
            # closing a descriptor while the owning thread is still running.
            socket.socket.shutdown(worker.stream, socket.SHUT_RDWR)
        except OSError as error:
            if error.errno != errno.ENOTCONN:
                self._clean = False

    def _fence(self, reason):
        with self._lock:
            self._fence_locked(reason)

    def _fence_locked(self, reason):
        if self._reason is not None:
            return
        self._reason = reason
        if self._borrowed:
            for site in self._sites:
                self._clean &= self._close_locked(site.listener, site.identity)
        for worker in self._workers:
            self._cancel_locked(worker)
        self._stopped.set()

    def close(self, *, timeout=5.0):
        """Fence, close and confirm both listener and service drain.

        On false, keep this handle and the reservation and owner contexts.
        A timed-out drain can be checked again, never restarted. TCP shutdown
        wakes I/O but cannot preempt arbitrary synchronous dispatch code.
        """
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("Invalid named HTTPS drain timeout.")
        deadline = monotonic() + timeout
        if not self._lifecycle.acquire(timeout=timeout):
            return False
        try:
            self._fence(StopReason.REQUESTED)
            with self._lock:
                threads = [self._monitor, *(worker.thread for worker in self._workers)]
            for thread in threads:
                if thread is not None and thread.ident is not None:
                    if thread is current_thread():
                        return False
                    thread.join(max(0.0, deadline - monotonic()))
                    if thread.is_alive():
                        return False
            with self._lock:
                clean = self._clean
                if clean:
                    self._workers.clear()
            try:
                drained = self._services.close(timeout=max(0.0, deadline - monotonic()))
            except Exception:  # noqa: BLE001 - never disclose cleanup diagnostics.
                return False
            return clean and drained is True
        finally:
            self._lifecycle.release()
