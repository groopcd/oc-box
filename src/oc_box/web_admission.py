"""In-memory admission for one named HTTPS web relay lifetime.

Internal trusted callers only. The caller verifies the owner, HTTPS transport,
request origin policy, and account authentication/workspace access. This module
does not verify credentials, manage cookies, or open an upstream.
"""

import re
import secrets
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Lock
from time import monotonic

if __package__:
    from .web_tls import parse_https_origin
else:
    from web_tls import parse_https_origin

_TOKEN_BYTES = 32
_TOKEN_LENGTH = 43  # Unpadded URL-safe base64 encoding of 32 random bytes.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class WebBinding:
    """Verified workspace and owner generation, plus the relay's HTTPS origin."""

    workspace_id: str
    owner_generation: str
    origin: str

    def __post_init__(self):
        if any(
            type(value) is not str or not 0 < len(value) <= 256
            for value in (self.workspace_id, self.owner_generation)
        ):
            raise ValueError("Invalid web relay binding.")
        if (
            type(self.origin) is not str
            or parse_https_origin(self.origin).value != self.origin
        ):
            raise ValueError("Expected a canonical HTTPS origin.")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class BrowserSession:
    """A lookup snapshot, not connection admission. Its identity is the generation."""

    principal: str
    expires_at: float
    _token: str


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ConnectionUse:
    """A registered, cancellable connection starting or already active."""

    _cancel: Callable[[], None]
    _cancelled: Event = field(default_factory=Event, init=False)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


@dataclass(slots=True)
class _Entry:
    session: BrowserSession
    connections: set[ConnectionUse] = field(default_factory=set)


def _check_limit(value, maximum, *, seconds=False):
    types = (int, float) if seconds else (int,)
    if type(value) not in types or not 0 < value <= maximum:
        raise ValueError("Invalid web admission limit.")


class BrowserAdmission:
    """One bound relay lifetime; closed instances cannot be reopened or moved.

    Session expiry is absolute and monotonic. Call ``expire()`` from the relay
    timer even when there is no request traffic. Call ``close()`` and drain
    the cancelled uses before releasing the bound owner lease. There is no
    background thread or transport cleanup wait here.
    """

    def __init__(
        self,
        binding: WebBinding,
        *,
        session_ttl: float = 3600,
        max_sessions: int = 128,
        max_connections: int = 128,
        attempt_limit: int = 12,
        attempt_window: float = 60,
    ):
        if type(binding) is not WebBinding:
            raise ValueError("Invalid web relay binding.")
        _check_limit(session_ttl, 86400, seconds=True)
        _check_limit(max_sessions, 1024)
        _check_limit(max_connections, 1024)
        _check_limit(attempt_limit, 1024)
        _check_limit(attempt_window, 3600, seconds=True)
        self._binding = binding
        self._session_ttl = session_ttl
        self._max_sessions = max_sessions
        self._max_connections = max_connections
        self._attempt_limit = attempt_limit
        self._attempt_window = attempt_window
        self._lock = Lock()
        self._sessions: dict[str, _Entry] = {}
        self._attempts: deque[float] = deque()
        self._in_use = 0
        self._closed = False
        self._cancellation_failed = False

    def _matches(self, binding):
        return type(binding) is WebBinding and binding == self._binding

    def _entry(self, session):
        if type(session) is BrowserSession:
            entry = self._sessions.get(session._token)
            # A fresh sign-in or registry never validates an older snapshot,
            # even if a token were to be reused.
            if entry is not None and entry.session is session:
                return entry
        return None

    def _retire(self, token, pending):
        """Under the lock, revoke and latch cancellation before notifying."""
        entry = self._sessions.pop(token)
        for connection in entry.connections:
            # Private Event: only this lock's holder sets it and readers only
            # call is_set(). No user callback or transport work runs here.
            connection._cancelled.set()
            pending.append(connection)
        entry.connections.clear()

    def _expired(self, now):
        pending = []
        for token, entry in tuple(self._sessions.items()):
            if entry.session.expires_at <= now:
                self._retire(token, pending)
        return pending

    def _notify(self, pending):
        for connection in pending:
            try:
                connection._cancel()
            except Exception:
                # Do not skip peers, retain raw errors, or undo revocation.
                with self._lock:
                    self._cancellation_failed = True

    @property
    def cancellation_failed(self) -> bool:
        """Whether any cancellation callback raised; not proof of upstream exit."""
        with self._lock:
            return self._cancellation_failed

    def try_attempt(self, binding: WebBinding) -> bool:
        """Charge one attempt before verifier work, regardless of its outcome.

        One shared rolling budget (no account/client keys), so one caller can
        exhaust it for everyone until it refills. This bounds admission, not
        body size, verifier duration, or protocol state.
        """
        if not self._matches(binding):
            return False
        with self._lock:
            if self._closed:
                return False
            now = monotonic()
            while self._attempts and self._attempts[0] <= now - self._attempt_window:
                self._attempts.popleft()
            if len(self._attempts) == self._attempt_limit:
                return False
            self._attempts.append(now)
            return True

    def issue(self, binding: WebBinding, *, principal: str) -> str | None:
        """Mint only after an independent, workspace-authorized sign-in."""
        if not self._matches(binding):
            return None
        if (
            type(principal) is not str
            or not 0 < len(principal) <= 128
            or not principal.isprintable()
        ):
            raise ValueError("Invalid authenticated web principal.")
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._lock:
            now = monotonic()
            pending = self._expired(now)
            if (
                self._closed
                or len(self._sessions) == self._max_sessions
                or token in self._sessions
            ):
                token = None
            else:
                session = BrowserSession(principal, now + self._session_ttl, token)
                self._sessions[token] = _Entry(session)
        self._notify(pending)
        return token

    def lookup(self, binding: WebBinding, token: str) -> BrowserSession | None:
        if (
            not self._matches(binding)
            or type(token) is not str
            or len(token) != _TOKEN_LENGTH
            or not _TOKEN.fullmatch(token)
        ):
            return None
        with self._lock:
            pending = self._expired(monotonic())
            entry = self._sessions.get(token)
        self._notify(pending)
        return entry.session if entry is not None else None

    @contextmanager
    def connection(
        self,
        binding: WebBinding,
        session: BrowserSession | None,
        *,
        cancel: Callable[[], None],
    ) -> Iterator[ConnectionUse | None]:
        """Yield a registered use, or None if the snapshot is no longer admissible.

        Entry is the linearization point: logout that wins the lock prevents
        admission; otherwise logout cancels an already registered use, including
        one still starting. Install a nonblocking, thread-safe cancellation
        callback bound to this unique transport before entry. It must be safe
        to run concurrently with, or just after, normal cleanup. The transport
        must honor cancellation before and during startup (not just close an
        already-open upstream), and finish cleanup before exiting this context.
        A check of ``cancelled`` alone does not make socket creation atomic.
        Keep lookup snapshots and uses out of delayed work queues.
        """
        if not self._matches(binding):
            yield None
            return
        if not callable(cancel):
            raise TypeError("Expected a web connection cancellation callback.")
        use = None
        with self._lock:
            pending = self._expired(monotonic())
            entry = self._entry(session)
            if entry is not None and self._in_use < self._max_connections:
                use = ConnectionUse(cancel)
                entry.connections.add(use)
                self._in_use += 1
        try:
            self._notify(pending)
            yield use if use is not None and not use.cancelled else None
        finally:
            if use is not None:
                with self._lock:
                    entry.connections.discard(use)
                    # A cancelled use still consumes capacity until it exits.
                    self._in_use -= 1

    def revoke(self, binding: WebBinding, session: BrowserSession) -> bool:
        """Revoke exactly the looked-up session, not other sign-ins."""
        if not self._matches(binding):
            return False
        with self._lock:
            pending = self._expired(monotonic())
            revoked = self._entry(session) is not None
            if revoked:
                self._retire(session._token, pending)
        self._notify(pending)
        return revoked

    def expire(self) -> None:
        """Revoke expired sessions and notify their registered connections."""
        with self._lock:
            pending = self._expired(monotonic())
        self._notify(pending)

    def close(self) -> None:
        """Permanently stop admission and cancel all remaining registered uses."""
        pending = []
        with self._lock:
            self._closed = True
            for token in tuple(self._sessions):
                self._retire(token, pending)
            self._attempts.clear()
        self._notify(pending)
