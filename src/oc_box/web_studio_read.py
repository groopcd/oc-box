"""Unserved Studio conversation route for a preauthenticated, registered grant."""

from dataclasses import dataclass, field
import json
import os
import socket
import struct
from threading import Event, Lock
from time import monotonic

if __package__:
    from . import web_admission, web_named_owner, web_named_relay
    from . import web_studio_conversation as studio
else:
    import web_admission
    import web_named_owner
    import web_named_relay
    import web_studio_conversation as studio


PATH = "/oc-box/studio/conversation"
_WAIT_SLICE = 0.05
_DRAIN_GRACE = 0.5


class ReadError(ValueError):
    """Fixed refusal; do not retry/rebind or emit a second HTTP response."""

    def __init__(self):
        super().__init__("The Studio conversation could not be verified.")


def _refuse():
    raise ReadError() from None


@dataclass(frozen=True, slots=True, repr=False)
class ReadRequest:
    """Only these fields cross from the parsed first-party request."""

    method: str
    target: str
    body: bytes


@dataclass(frozen=True, slots=True, repr=False)
class ReadScope:
    """Trusted grant and exact selection snapshot, never a browser assertion."""

    owner: web_named_owner.OwnerIdentity
    browser: web_admission.BrowserSession = field(compare=False)
    session_id: str
    selection_generation: int

    def __post_init__(self):
        if (
            type(self.owner) is not web_named_owner.OwnerIdentity
            or self.owner.version != studio.NATIVE_VERSION
            or type(self.browser) is not web_admission.BrowserSession
            or type(self.session_id) is not str
            or not studio._session_id(self.session_id)
            or type(self.selection_generation) is not int
            or not 0 <= self.selection_generation <= 2**53 - 1
        ):
            _refuse()


@dataclass(frozen=True, slots=True, repr=False)
class ReadResponse:
    """First-party result. The writer owns length, framing and connection."""

    body: bytes
    status = 200
    headers = (
        ("Content-Type", "application/json; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    )


class _Bridge:
    """Retain the exact exec handle; stdout alone never proves cleanup."""

    def __init__(self):
        self._child = web_named_relay._WorkspaceChild()
        self.confirmed = True

    def read(self, command, deadline, *, current, check):
        received = bytearray()
        eof = False

        def finish(peer, end, live):
            nonlocal eof
            while not eof:
                live()
                left = studio._remaining(end)
                room = studio.MAX_OUTPUT_BYTES + 2 - len(received)
                if room <= 0:
                    _refuse()
                peer.settimeout(min(left, _WAIT_SLICE))
                try:
                    chunk = peer.recv(min(65536, room))
                except TimeoutError:
                    continue
                received.extend(chunk)
                eof = not chunk
            self._child.finish(end, live)  # Actual zero exit of the exact exec.
            if not (
                received == studio._READ_REFUSED
                or (
                    received[:1] == studio._READ_READY
                    and 1 < len(received) <= studio.MAX_OUTPUT_BYTES + 1
                )
            ):
                _refuse()
            self.confirmed = True

        peer, stdio = socket.socketpair()
        try:
            with peer, stdio:
                try:
                    current()
                    self.confirmed = False
                    self._child.start(command, stdio)
                    stdio.close()
                    # The child has not sent any native requests yet. It
                    # needs the private start byte after this second check.
                    current()
                    peer.settimeout(studio._remaining(deadline))
                    check()
                    peer.sendall(studio._START_READ)
                    finish(peer, deadline, check)
                finally:
                    stdio.close()
                    if not self.confirmed:
                        try:
                            peer.shutdown(socket.SHUT_WR)
                            finish(peer, monotonic() + _DRAIN_GRACE, lambda: None)
                        except Exception:  # Cleanup-only; no late payload release.
                            pass
        finally:
            try:
                self._child.close()
            except Exception:
                self.confirmed = False
                _refuse()
        if received[:1] != studio._READ_READY:
            _refuse()
        return bytes(received[1:])


class _Operation:
    def __init__(self, interrupt):
        self.bridge = _Bridge()
        self.cancelled = Event()
        self._lock = Lock()
        self._interrupt = interrupt

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            if self._interrupt is not None:
                self._interrupt()

    def disarm(self):
        with self._lock:
            self._interrupt = None


def respond(
    box,
    request,
    scope,
    *,
    lease,
    admission,
    binding,
    observe,
    deadline,
    write,
    interrupt,
    retire,
):
    """Read and deliver synchronously under the actual browser-use registration.

    The producer enforces ingress/auth and holds the original verified owner
    lease. ``observe(deadline)`` must re-read the exact live approved browser
    selection (including its change generation) without adopting a new one.
    ``write(reply, deadline, *, check)`` must check before each bounded write.
    ``interrupt()`` must nonblockingly wake this exact request's downstream
    I/O, including a cancel during startup. ``retire()`` must nonblockingly
    retire that transport and return True only when no downstream work
    remains. No late or deferred delivery.
    """
    try:
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(request) is not ReadRequest
            or type(scope) is not ReadScope
            or type(lease) is not web_named_owner.VerifiedOwnerLease
            or type(admission) is not web_admission.BrowserAdmission
            or type(binding) is not web_admission.WebBinding
            or scope.owner != lease.identity
            or binding.workspace_id != scope.owner.workspace_id
            or binding.owner_generation != scope.owner.generation
            or type(request.method) is not str
            or request.method != "GET"
            or type(request.target) is not str
            or request.target != PATH + "?session=" + scope.session_id
            or type(request.body) is not bytes
            or request.body
            or not all(callable(func) for func in (observe, write, interrupt, retire))
            or type(deadline) not in (int, float)
        ):
            _refuse()
        studio._remaining(deadline)
        deadline = min(
            deadline, scope.browser.expires_at, monotonic() + studio.HOST_TIMEOUT
        )
        control = lease.control
        control.getpeername()
        _, uid, _ = struct.unpack(
            "3i", control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != os.getuid():
            _refuse()
        descriptor = web_named_owner._descriptor(control)
        op = _Operation(interrupt)
        with admission.connection(binding, scope.browser, cancel=op.cancel) as use:
            try:

                def check():
                    if (
                        use is None
                        or use.cancelled
                        or op.cancelled.is_set()
                        or not web_named_owner._quiet_control(control, descriptor)
                    ):
                        _refuse()
                    studio._remaining(deadline)

                def current():
                    check()
                    owner = lease.observe()
                    if (
                        type(owner) is not web_named_owner.OwnerIdentity
                        or owner != scope.owner
                    ):
                        _refuse()
                    check()
                    live = observe(deadline)
                    if (
                        type(live) is not ReadScope
                        or live != scope
                        or live.browser is not scope.browser
                    ):
                        _refuse()
                    check()

                owner = scope.owner
                current()
                value = studio._read_conversation(
                    box,
                    {
                        "id": owner.workspace_id,
                        "container": owner.container_id,
                        "project": lease.project,
                        "home": lease.home,
                        "opencode_version": owner.version,
                    },
                    {
                        "workspace": owner.workspace_id,
                        "version": owner.version,
                        "url": owner.endpoint,
                        "password": lease.native_password,
                    },
                    scope.session_id,
                    deadline=deadline,
                    bridge=lambda command, end: op.bridge.read(
                        command, end, current=current, check=check
                    ),
                    managed=True,
                )
                body = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if (
                    not op.bridge.confirmed
                    or not 0 < len(body) <= studio.MAX_OUTPUT_BYTES
                ):
                    _refuse()
                current()
                write(ReadResponse(body), deadline, check=current)
                current()
            finally:
                retired = False
                try:
                    op.disarm()
                    if use is not None:
                        retired = retire() is True
                finally:
                    if use is not None and (not retired or not op.bridge.confirmed):
                        # Keep this use charged with its exact child handle,
                        # even if logout raced cleanup. Fence later admissions.
                        use._cleanup_unconfirmed.set()
                        admission.close()
                        _refuse()
        return
    except Exception:  # Do not expose private worker, selection or writer errors.
        pass
    _refuse()
