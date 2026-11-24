"""Unserved first-party Studio admission on one original legacy loopback gateway.

Trusted host adapter only. This module owns no listener, owner lease, native
connector, browser routes, or credentials. See docs/plans/web-studio-guest.md.
"""

import base64
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
import os
import re
import secrets
from threading import Condition, Event, Lock
from time import monotonic


CONFIRM_PATH = "/auth/oc-box-studio-guest/confirm"  # Reserved contract, not served.
_ORIGIN = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_HEADER = re.compile(r"[A-Za-z0-9-]{1,64}")
_FORBIDDEN_HEADERS = {
    "authorization",
    "forwarded",
    "transfer-encoding",
    "upgrade",
    "trailer",
    "expect",
}
_MAX_HEADER_BYTES = 8192
_MAX_COOKIE_BYTES = 4096
_MAX_ORDINAL = 2**64 - 1
_ATTEMPT_WINDOW = 60
_REQUEST_SECONDS = 10


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestBinding:
    """Trusted reservation handle; values alone never prove a live owner."""

    workspace_id: str
    owner_generation: str
    container_id: str
    origin: str
    gateway_generation: str = field(
        default_factory=lambda: secrets.token_hex(16), init=False
    )
    _claim_lock: Lock = field(default_factory=Lock, init=False)
    _claimed: Event = field(default_factory=Event, init=False)

    def __post_init__(self):
        if (
            any(
                type(value) is not str
                or len(value) != size
                or re.fullmatch(r"[a-f0-9]+", value) is None
                for value, size in (
                    (self.workspace_id, 32),
                    (self.owner_generation, 32),
                    (self.container_id, 64),
                )
            )
            or type(self.origin) is not str
            or not 18 <= len(self.origin) <= 22
            or (match := _ORIGIN.fullmatch(self.origin)) is None
            or int(match[1]) > 65535
        ):
            raise ValueError("Invalid Studio guest gateway binding.")

    @property
    def authority(self):
        return self.origin.removeprefix("http://")


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmationRequest:
    """One bounded, fully framed request from the original trusted ingress."""

    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestChallenge:
    value: str


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestGrant:
    """Exact server-side grant identity, not a named grant or a native cookie."""

    expires_at: float


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestBootstrap:
    """Deliver only app/page to the document; grant is the private identity."""

    app: str
    page: str
    grant: GuestGrant


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestUse:
    deadline: float
    _cancelled: Event = field(default_factory=Event, init=False)

    @property
    def cancelled(self):
        return self._cancelled.is_set()


@dataclass(slots=True, repr=False)
class _Active:
    cancel: Callable[[], None]
    grant: GuestGrant | None
    checking: bool = False
    uncertain: bool = False


def _token(value):
    return (
        type(value) is str and len(value) == 43 and _TOKEN.fullmatch(value) is not None
    )


def _digest(value):
    return sha256(value.encode("ascii")).digest()


def _limit(value, maximum, *, seconds=False):
    return type(value) in ((int, float) if seconds else (int,)) and 0 < value <= maximum


def _deadline(value, now):
    if type(value) in (int, float) and now < value < float("inf"):
        return min(value, now + _REQUEST_SECONDS)
    return None


def _confirmation(binding, request):
    if (
        type(request) is not ConfirmationRequest
        or type(request.method) is not str
        or request.method != "POST"
        or type(request.target) is not str
        or request.target != CONFIRM_PATH
        or type(request.body) is not bytes
        or len(request.body) != 43
        or not request.body.isascii()
        or not _token(challenge := request.body.decode("ascii"))
        or type(request.headers) is not tuple
        or len(request.headers) > 32
    ):
        return None
    fields = {}
    size = 0
    for pair in request.headers:
        if type(pair) is not tuple or len(pair) != 2:
            return None
        name, value = pair
        if (
            type(name) is not str
            or len(name) > 64
            or _HEADER.fullmatch(name) is None
            or type(value) is not str
            or len(value) > _MAX_COOKIE_BYTES
            or any(not 32 <= ord(char) <= 126 for char in value)
        ):
            return None
        size += len(name) + len(value) + 4
        name = name.lower()
        if (
            size > _MAX_HEADER_BYTES
            or name in fields
            or name in _FORBIDDEN_HEADERS
            or name == "connection"
            and value.lower() not in ("close", "keep-alive")
            or name.startswith(("proxy-", "x-forwarded-", "access-control-request-"))
        ):
            return None
        fields[name] = value
    cookie = fields.get("cookie")
    if (
        fields.get("host") != binding.authority
        or fields.get("origin") != binding.origin
        or fields.get("sec-fetch-site") != "same-origin"
        or fields.get("sec-fetch-mode") != "same-origin"
        or fields.get("sec-fetch-dest") != "empty"
        or fields.get("content-type") != "text/plain"
        or fields.get("content-length") != str(len(request.body))
        or not cookie
        or not cookie.strip(" ")
    ):
        return None
    # Header block shape for the legacy native authenticator, not a whole wire
    # request. Cookie syntax is not authority; only the native verifier decides.
    headers = (
        "GET /api/info HTTP/1.1\r\nhost: "
        + binding.authority
        + "\r\norigin: "
        + binding.origin
        + "\r\ncookie: "
        + cookie
    ).encode("ascii")
    return _digest(challenge), headers


class GuestAdmission:
    """One guest-only registry, never a named HTTPS or native session registry.

    Pin ``observe_gateway(binding, *, use, deadline)`` and
    ``verify_native(binding, *, use, headers, authority, deadline)`` at
    construction, not from a request. Each must return the original binding
    object on positive verification and None on uncertainty. The verifier
    must authenticate the forwarded Cookie against the original native
    service in that gateway and positively verify its worker cleanup before
    returning success. Both run synchronously, honor cancellation and the
    absolute deadline, and may not save request credentials or do fallback.
    """

    def __init__(
        self,
        binding,
        *,
        observe_gateway,
        verify_native,
        challenge_ttl=30,
        grant_ttl=900,
        max_pending=32,
        max_grants=64,
        max_uses=32,
        attempt_limit=12,
    ):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(binding) is not GuestBinding
            or not callable(observe_gateway)
            or not callable(verify_native)
            or not _limit(challenge_ttl, 60, seconds=True)
            or not _limit(grant_ttl, 3600, seconds=True)
            or not _limit(max_pending, 128)
            or not _limit(max_grants, 256)
            or not _limit(max_uses, 128)
            or not _limit(attempt_limit, 128)
        ):
            raise ValueError("Invalid Studio guest admission configuration.")
        with binding._claim_lock:
            if binding._claimed.is_set():
                raise ValueError(
                    "Studio guest gateway already has an admission lifetime."
                )
            binding._claimed.set()
        self._binding = binding
        self._observe_gateway = observe_gateway
        self._verify_native = verify_native
        self._challenge_ttl = challenge_ttl
        self._grant_ttl = grant_ttl
        self._max_pending = max_pending
        self._max_grants = max_grants
        self._max_uses = max_uses
        self._attempt_limit = attempt_limit
        self._condition = Condition()
        self._pending: dict[bytes, float] = {}
        self._grants: dict[tuple[bytes, bytes], GuestGrant] = {}
        self._uses: dict[GuestUse, _Active] = {}
        self._attempts: deque[float] = deque()
        self._ordinal = 0
        self._closed = False
        self._failed = False
        self._notifications = 0

    def _mint(self):
        """Under the lock, never repeat a bearer even if entropy repeats."""
        if self._ordinal == _MAX_ORDINAL:
            return None
        try:
            random = secrets.token_bytes(24)
        except OSError:
            return None
        self._ordinal += 1
        # Domain-separate listener generations as well as uses within one
        # listener. The generation is not a credential.
        prefix = sha256(
            bytes.fromhex(self._binding.gateway_generation) + random
        ).digest()[:24]
        raw = prefix + self._ordinal.to_bytes(8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def _cancel(self, use, entry, pending):
        if not use.cancelled:
            use._cancelled.set()
            self._notifications += 1
            pending.append(entry.cancel)

    def _sweep(self, now, pending):
        for key, expiry in tuple(self._pending.items()):
            if expiry <= now:
                del self._pending[key]
        for key, grant in tuple(self._grants.items()):
            if grant.expires_at <= now:
                del self._grants[key]
        for use, entry in self._uses.items():
            if use.deadline <= now or (
                entry.grant is not None and entry.grant.expires_at <= now
            ):
                self._cancel(use, entry, pending)

    def _fence(self, pending):
        self._closed = True
        self._pending.clear()
        self._grants.clear()
        self._attempts.clear()
        for use, entry in self._uses.items():
            self._cancel(use, entry, pending)

    def _use_room(self):
        # A finished exchange can still have notification delivery pending.
        return len(self._uses) + self._notifications < self._max_uses

    def _notify(self, pending):
        # Cancellation is latched under the condition before delivery. A
        # concurrent finish cannot make drain true while delivery is pending.
        while pending:
            cancel = pending.pop()
            failed = True
            try:
                cancel()
                failed = False
            except Exception:
                pass  # Never retain private transport diagnostics.
            finally:
                with self._condition:
                    self._notifications -= 1
                    if failed:
                        self._failed = True
                        self._fence(pending)
                    self._condition.notify_all()

    def offer(self, binding):
        """Mint one no-authority challenge for the original confirmation page."""
        if binding is not self._binding:
            return None
        pending = []
        token = None
        with self._condition:
            now = monotonic()
            self._sweep(now, pending)
            if not self._closed and len(self._pending) < self._max_pending:
                token = self._mint()
                if token is not None:
                    self._pending[_digest(token)] = now + self._challenge_ttl
        self._notify(pending)
        return GuestChallenge(token) if token is not None else None

    def _begin_confirmation(self, key, deadline, cancel):
        pending = []
        use = None
        with self._condition:
            now = monotonic()
            self._sweep(now, pending)
            end = _deadline(deadline, now)
            # A matching, well-framed attempt burns the challenge on refusal
            # too. Malformed or cross-origin input never reaches this point.
            expiry = self._pending.pop(key, None) if end is not None else None
            if not self._closed and expiry is not None:
                while self._attempts and self._attempts[0] <= now - _ATTEMPT_WINDOW:
                    self._attempts.popleft()
                if len(self._attempts) < self._attempt_limit:
                    self._attempts.append(now)
                    if self._use_room() and len(self._grants) < self._max_grants:
                        use = GuestUse(min(end, expiry))
                        self._uses[use] = _Active(cancel, None)
        self._notify(pending)
        return use

    def confirm(self, binding, request, *, cancel, deadline):
        """Return None if unregistered, or (use, bootstrap-or-None).

        For any returned use the caller must finish the exact exchange, even
        if native authentication failed. Only a bootstrap and a still-current
        use authorize grant delivery. Do not retain the raw request.
        """
        if binding is not self._binding or not callable(cancel):
            return None
        checked = _confirmation(binding, request)
        if checked is None:
            return None
        key, headers = checked
        use = self._begin_confirmation(key, deadline, cancel)
        if use is None:
            return None
        if not self.current(use):
            return use, None
        _, verified = self._probe(
            use, self._verify_native, headers=headers, authority=binding.authority
        )
        if not self.current(use) or verified is not binding:
            return use, None
        with self._condition:
            if not self._live(use) or len(self._grants) >= self._max_grants:
                return use, None
            app, page = self._mint(), self._mint()
            if app is None or page is None:
                return use, None
            grant = GuestGrant(monotonic() + self._grant_ttl)
            self._grants[_digest(app), _digest(page)] = grant
            self._uses[use].grant = grant
            return use, GuestBootstrap(app, page, grant)

    def start(self, binding, *, app, page, cancel, deadline):
        """Match both bearers; return None if unregistered or (use, exact grant).

        The adapter must independently already enforce its first-party request
        policy. A registered but now stale use returns (use, None); finish it.
        """
        if (
            binding is not self._binding
            or not _token(app)
            or not _token(page)
            or not callable(cancel)
        ):
            return None
        pending = []
        use = None
        with self._condition:
            now = monotonic()
            self._sweep(now, pending)
            end = _deadline(deadline, now)
            grant = self._grants.get((_digest(app), _digest(page)))
            if (
                not self._closed
                and end is not None
                and grant is not None
                and self._use_room()
            ):
                use = GuestUse(min(end, grant.expires_at))
                self._uses[use] = _Active(cancel, grant)
        self._notify(pending)
        return (use, grant if self.current(use) else None) if use is not None else None

    def _live(self, use):
        if self._closed or type(use) is not GuestUse or use.cancelled:
            return False
        entry = self._uses.get(use)
        now = monotonic()
        return (
            entry is not None
            and now < use.deadline
            and (entry.grant is None or now < entry.grant.expires_at)
        )

    def _probe(self, use, callback, **arguments):
        # A registered use covers its synchronous checks too. Even a caller
        # that retires both streams too early cannot discharge the use while
        # the observer/verifier can still act on the original lease.
        with self._condition:
            if not self._live(use) or (entry := self._uses[use]).checking:
                return False, None
            entry.checking = True
            deadline = use.deadline
            if entry.grant is not None:
                deadline = min(deadline, entry.grant.expires_at)
        try:
            return True, callback(
                self._binding, use=use, deadline=deadline, **arguments
            )
        except Exception:
            return True, None
        finally:
            with self._condition:
                entry.checking = False

    def current(self, use):
        """Recheck the original owner for this exact registered transport."""
        self.expire()
        called, observed = self._probe(use, self._observe_gateway)
        if not called:
            return False
        if observed is not self._binding:
            self.close()
            return False
        self.expire()
        with self._condition:
            return self._live(use)

    def revoke(self, binding, grant):
        """Revoke this exact authenticated guest grant, never a native session."""
        if binding is not self._binding or type(grant) is not GuestGrant:
            return False
        pending = []
        revoked = False
        with self._condition:
            self._sweep(monotonic(), pending)
            for key, current in tuple(self._grants.items()):
                if current is grant:
                    del self._grants[key]
                    revoked = True
                    for use, entry in self._uses.items():
                        if entry.grant is grant:
                            self._cancel(use, entry, pending)
                    break
        self._notify(pending)
        return revoked

    def finish(self, use, *, confirmed):
        """Discharge a use only after real downstream and native retirement."""
        if type(use) is not GuestUse:
            return False
        pending = []
        with self._condition:
            entry = self._uses.get(use)
            if entry is None or entry.uncertain:
                return False
            if confirmed is True and not entry.checking:
                # Trusted transport evidence, never a wire field.
                del self._uses[use]
                self._condition.notify_all()
                return True
            entry.uncertain = self._failed = True
            self._fence(pending)
            self._condition.notify_all()
        self._notify(pending)
        return False

    def expire(self):
        """Called by the existing gateway timer, including during idle."""
        pending = []
        with self._condition:
            self._sweep(monotonic(), pending)
        self._notify(pending)

    def close(self):
        """Fence new work and request cancellation; does not release the lease."""
        pending = []
        with self._condition:
            self._fence(pending)
        self._notify(pending)

    def wait_drained(self, timeout=5.0):
        """After close, require all exact uses and notifications to be confirmed."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("Invalid Studio guest drain deadline.")
        with self._condition:
            if not self._closed:
                return False
            return (
                self._condition.wait_for(
                    lambda: self._failed or not (self._uses or self._notifications),
                    timeout,
                )
                and not self._failed
            )
