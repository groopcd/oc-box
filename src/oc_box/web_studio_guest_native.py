"""Unserved managed native-cookie confirmation for an independently verified guest."""

from dataclasses import dataclass
import os
from threading import Event, Lock
from time import monotonic

if __package__:
    from . import web_named_relay, web_native_pair, web_studio_guest
else:
    import web_named_relay
    import web_native_pair
    import web_studio_guest


NATIVE_VERSION = "2.4.1"
_LINE = b"GET /api/info HTTP/1.1"
_MAX_COOKIE = 4096
_MAX_RESPONSE = 8192 + 4 + 8192
_SECONDS = 6
_HEADERS = {
    "access-control-allow-origin",
    "connection",
    "content-length",
    "content-type",
    "date",
    "vary",
}


class GuestNativeError(ValueError):
    """Fixed refusal; response, scope and credential details never escape."""

    def __init__(self):
        super().__init__(
            "Studio guest native cookie authentication could not be verified."
        )


def _refuse():
    raise GuestNativeError() from None


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestNativeScope:
    """Trusted native facts of the original gateway lease, not an attestation."""

    binding: web_studio_guest.GuestBinding
    endpoint: str
    native_pid: int
    version: str

    def __post_init__(self):
        try:
            port = web_native_pair._port(self.endpoint)
        except web_native_pair.NativePairError:
            _refuse()
        if (
            type(self.binding) is not web_studio_guest.GuestBinding
            or self.endpoint != f"http://127.0.0.1:{port}"
            or type(self.native_pid) is not int
            or not 0 < self.native_pid <= 2**31 - 1
            or type(self.version) is not str
            or self.version != NATIVE_VERSION
        ):
            _refuse()


def _host_origin(binding):
    return (
        b"host: "
        + binding.authority.encode("ascii")
        + b"\r\norigin: "
        + binding.origin.encode("ascii")
    )


def _cookie(binding, authority, headers):
    # Exact Go/No-go seam from GuestAdmission. No accepted alternate field
    # order, auth header, target, request body, origin rewrite or appended data.
    prefix = _LINE + b"\r\n" + _host_origin(binding) + b"\r\ncookie: "
    if (
        type(authority) is not str
        or authority != binding.authority
        or type(headers) is not bytes
        or not len(prefix) < len(headers) <= len(prefix) + _MAX_COOKIE
        or not headers.startswith(prefix)
    ):
        _refuse()
    value = headers[len(prefix) :]
    if not value.strip(b" ") or any(not 32 <= byte <= 126 for byte in value):
        _refuse()
    return value


def _wire(binding, cookie):
    return (
        _LINE
        + b"\r\n"
        + _host_origin(binding)
        + (b"\r\ncookie: " + cookie if cookie is not None else b"")
        + b"\r\nAccept: application/json\r\nAccept-Encoding: identity"
        + b"\r\nConnection: close\r\n\r\n"
    )


def _reply(raw, scope, *, authenticated):
    status, headers, body = web_native_pair._reply(raw)
    expected = _HEADERS if authenticated else _HEADERS | {"www-authenticate"}
    if (
        not raw.startswith(b"HTTP/1.1 ")
        or status != (200 if authenticated else 401)
        or headers.keys() - expected
        or any(len(values) != 1 for values in headers.values())
        or headers.get("access-control-allow-origin") != [scope.binding.origin]
        or headers.get("connection") != ["close"]
        or headers.get("content-type") != ["application/json"]
        or headers.get("content-length") != [str(len(body))]
        or not body
    ):
        _refuse()
    value = web_native_pair.named_native_json(body)
    if authenticated:
        identity = web_native_pair.named_native_info(
            value, scope.version, scope.endpoint
        )
        if identity["pid"] != scope.native_pid:
            _refuse()
    elif headers.get("www-authenticate") != ['Basic realm="Secure Area"'] or value != {
        "_tag": "UnauthorizedError",
        "message": "Authentication required",
    }:
        # Recognize the native 401, never answer its advertised challenge.
        _refuse()


class ManagedGuestCookieVerifier:
    """One exact GuestAdmission verification attempt, with no retry or fallback.

    The adapter creates one before registering its exchange and binds
    ``cancel`` to the registered callback alongside downstream cancellation.
    Its fixed dispatch to this object must be synchronous and request-local.
    The adapter retains the original verified listener/owner lease, and uses
    ``cleanup_confirmed`` together with downstream evidence at ``finish``.
    """

    def __init__(self, box, scope, *, observe_gateway):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(scope) is not GuestNativeScope
            or not callable(observe_gateway)
        ):
            _refuse()
        self._box = box
        self._scope = scope
        self._observe = observe_gateway
        self._lock = Lock()
        self._cancelled = Event()
        self._used = False
        self._active = None
        self._cleanup_confirmed = True

    @property
    def cleanup_confirmed(self):
        with self._lock:
            return self._cleanup_confirmed

    def cancel(self):
        """Fence both probe starts and interrupt only this attempt's active I/O."""
        with self._lock:
            self._cancelled.set()
            if self._active is not None:
                self._active.cancel()

    def _time_left(self, use, deadline):
        left = deadline - monotonic()
        if self._cancelled.is_set() or use.cancelled or left <= 0:
            _refuse()
        return left

    def _current(self, use, deadline):
        self._time_left(use, deadline)
        binding = self._scope.binding
        if self._observe(binding, use=use, deadline=deadline) is not binding:
            _refuse()
        return self._time_left(use, deadline)

    def _exchange(self, use, deadline, cookie):
        scope = self._scope
        io = web_named_relay._RegisteredIO(None, owns_browser=False)
        with self._lock:
            self._time_left(use, deadline)
            self._active = io
            self._cleanup_confirmed = False
        eof = False
        try:

            def current():
                return self._current(use, deadline)

            # Only the existing process-registered providerless connector.
            # No direct socket, connector injection, host port dial, or fallback.
            web_named_relay._ConnectorRegistration(
                io, scope.endpoint, current
            ).start_workspace_relay(
                self._box,
                container_id=scope.binding.container_id,
                endpoint=scope.endpoint,
            )
            if (
                io._workspace is None
                or not io._workspace._attempted
                or io.upstream is None
            ):
                _refuse()
            peer = io.upstream
            peer.settimeout(current())
            peer.sendall(_wire(scope.binding, cookie))
            raw = bytearray()
            while True:
                peer.settimeout(current())
                part = peer.recv(min(4096, _MAX_RESPONSE + 1 - len(raw)))
                if not part:
                    # A local shutdown from cancellation can also return b"".
                    # Never count that as observed EOF from the real relay.
                    with self._lock:
                        if self._cancelled.is_set() or use.cancelled:
                            _refuse()
                        eof = True
                    break
                raw.extend(part)
                if len(raw) > _MAX_RESPONSE:
                    _refuse()
            io.finish_workspace(deadline, current)
            return bytes(raw)
        finally:
            clean = False
            try:
                io.close()
                attempted = io._workspace is not None and io._workspace._attempted
                clean = io.cleanup_confirmed and (not attempted or eof)
            finally:
                with self._lock:
                    # Keep exact resources pinned on uncertain retirement.
                    if clean:
                        self._active = None
                    self._cleanup_confirmed = clean
            if not clean:
                _refuse()

    def __call__(self, binding, *, use, headers, authority, deadline):
        """Return the original GuestBinding on success, otherwise None."""
        with self._lock:
            if self._used:
                return None
            self._used = True
        now = monotonic()
        if (
            binding is not self._scope.binding
            or type(use) is not web_studio_guest.GuestUse
            or type(use.deadline) not in (int, float)
            or type(deadline) not in (int, float)
            or not now < deadline <= use.deadline < float("inf")
        ):
            return None
        deadline = min(deadline, now + _SECONDS)
        try:
            cookie = _cookie(binding, authority, headers)
            # Reject a service whose info is public even if it emits a
            # perfectly formed identity. Never send a credential on that path.
            _reply(
                self._exchange(use, deadline, None), self._scope, authenticated=False
            )
            _reply(
                self._exchange(use, deadline, cookie), self._scope, authenticated=True
            )
            self._current(use, deadline)
            return binding
        except Exception:
            # No raw errors, cookie, identity response, child output or retry.
            return None
