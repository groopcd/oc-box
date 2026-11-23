"""Offline orchestration for a future named HTTPS sign-in.

No PAKE implementation, HTTP handlers, enrollment, password input or native
pairing. A trusted host caller verifies TLS, request origin and the live owner
before calling this flow. It supplies a browser nonce independently minted
with ``secrets.token_urlsafe(32)`` and bound to the browser by the relay (not
from an untrusted request body or URL). The challenge ID selects a transaction
within that browser. These are server-side bindings, not OPAQUE transcript
validation or a substitute for the relay's cookie and origin checks.
"""

import base64
import re
import secrets
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic
from typing import Protocol

if __package__:
    from . import web_accounts
    from .web_admission import BrowserAdmission, WebBinding
    from .web_tls import parse_https_origin
else:
    import web_accounts
    from web_admission import BrowserAdmission, WebBinding
    from web_tls import parse_https_origin

MAX_MESSAGE_BYTES = 4096

# Canonical unpadded base64url for 32 bytes, including zero trailing pad bits.
_TOKEN_BYTES = 32
_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")


class OpaqueExchange(Protocol):
    """Host-only handle; native private state stays inside its verifier."""

    def finish(self, ke3: bytes) -> bool:
        """Verify at most once, with a bounded lifetime and clean worker exit."""
        ...

    def discard(self) -> None:
        """Cancel and reap, idempotently and safely against concurrent finish."""
        ...


class OpaqueServer(Protocol):
    """Seam for a reviewed, fixed-suite RFC 9807 server adapter, not a fallback.

    ``registration=None`` must use the library's fake-record path, with the
    same KE2 format and comparable work as real lookup. The adapter is trusted
    to validate protocol messages, perform bounded local work, and never log
    inputs. It must be thread-safe and return no serialized server state or key.
    The orchestration cannot establish timing indistinguishability.
    """

    def start(
        self,
        *,
        setup: bytes,
        credential_identifier: bytes,
        context: bytes,
        registration: bytes | None,
        ke1: bytes,
        deadline: float,
    ) -> tuple[bytes, OpaqueExchange]:
        """Return (KE2, live handle); clean up internally if start fails."""
        ...


@dataclass(frozen=True, slots=True)
class MessageSizes:
    """Exact wire byte lengths from the pinned suite, never client-selected."""

    ke1: int
    ke2: int
    ke3: int

    def __post_init__(self):
        for size in (self.ke1, self.ke2, self.ke3):
            if type(size) is not int or not 0 < size <= MAX_MESSAGE_BYTES:
                raise ValueError("Invalid web PAKE message sizes.")


@dataclass(frozen=True, slots=True, repr=False)
class PakeChallenge:
    challenge_id: str
    ke2: str  # Canonical padded standard base64 (like KE1 and KE3).
    context: str  # Public, canonical base64 SHA-256 transcript binding.


@dataclass(frozen=True, slots=True, repr=False)
class _Challenge:
    expires_at: float
    browser_nonce: str
    principal: str | None
    exchange: OpaqueExchange


def _b64(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value, size):
    if type(value) is not str or len(value) != ((size + 2) // 3) * 4:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    return raw if len(raw) == size and _b64(raw) == value else None


def _token(value):
    return (
        type(value) is str and len(value) == 43 and _TOKEN.fullmatch(value) is not None
    )


def _context(auth_origin, app_origin, challenge):
    # Do not put the browser cookie, owner credential or private workspace ID
    # in the public transcript. Those bindings are enforced in host state.
    parts = (b"oc-box.named.local/OPAQUE-25519-SHA512-v1",) + tuple(
        text.encode("ascii") for text in (auth_origin, app_origin, challenge)
    )
    return sha256(
        b"".join(len(part).to_bytes(2, "big") + part for part in parts)
    ).digest()


def _registrations(store, workspace_id):
    # AccountStore is intended to come from load(), but can be constructed by
    # a caller. Recheck bounds before building the scope-filtered lookup.
    web_accounts._scope((workspace_id,))
    if (
        type(store) is not web_accounts.AccountStore
        or type(store.accounts) is not tuple
        or len(store.accounts) > web_accounts.MAX_ACCOUNTS
    ):
        raise ValueError("Invalid web account snapshot.")
    web_accounts._protocol_bytes(store.setup, web_accounts.MAX_SETUP_BYTES)
    seen = set()
    registrations = {}
    for account in store.accounts:
        if (
            type(account) is not web_accounts.Account
            or type(account.workspace_ids) is not tuple
        ):
            raise ValueError("Invalid web account snapshot.")
        name = web_accounts._username(account.username)
        scope = web_accounts._scope(account.workspace_ids)
        web_accounts._protocol_bytes(
            account.registration, web_accounts.MAX_REGISTRATION_BYTES
        )
        if name in seen:
            raise ValueError("Invalid web account snapshot.")
        seen.add(name)
        if workspace_id in scope:
            registrations[name] = account.registration
    return registrations


class PakeFlow:
    """One verified owner/origin and one immutable web account snapshot.

    All request refusals are None. Map them uniformly and keep protocol bodies,
    state, tokens and usernames out of logs. Relay body/read/concurrency limits
    are still required: this layer bounds calls and retained bytes, not the
    adapter's per-call runtime or incoming HTTP traffic.

    Close this flow (including its BrowserAdmission) on account/scope change
    or owner loss. Call expire() periodically to drop idle challenge/session
    references. Close cancels owned exchanges; a concurrent start that has
    not returned a handle must clean up on return. The caller still drains all
    in-flight methods and cancelled transports before owner release.
    """

    def __init__(
        self,
        binding: WebBinding,
        *,
        store: web_accounts.AccountStore,
        backend: OpaqueServer,
        sizes: MessageSizes,
        auth_origin: str,
        challenge_ttl: float = 30,
        max_challenges: int = 32,
        max_in_flight: int = 2,
    ):
        if (
            type(binding) is not WebBinding
            or type(sizes) is not MessageSizes
            or type(auth_origin) is not str
        ):
            raise ValueError("Invalid web PAKE configuration.")
        auth, app = parse_https_origin(auth_origin), parse_https_origin(binding.origin)
        if auth.value != auth_origin or auth.hostname == app.hostname:
            raise ValueError("A separate canonical HTTPS account origin is required.")
        if (
            type(challenge_ttl) not in (int, float)
            or not 0 < challenge_ttl <= 60
            or type(max_challenges) is not int
            or not 0 < max_challenges <= 128
            or type(max_in_flight) is not int
            or not 0 < max_in_flight <= 8
        ):
            raise ValueError("Invalid web PAKE limits.")
        if not callable(getattr(backend, "start", None)):
            raise TypeError("Expected a web PAKE server adapter.")
        self._registrations = _registrations(store, binding.workspace_id)
        self._setup = store.setup
        self._binding = binding
        self._auth_origin = auth_origin
        self._admission = BrowserAdmission(binding)
        self._backend = backend
        self._sizes = sizes
        self._ttl = challenge_ttl
        self._max_challenges = max_challenges
        self._max_in_flight = max_in_flight
        self._lock = Lock()
        # A None slot is reserved by an active start or a consumed finish.
        self._entries: dict[str, _Challenge | None] = {}
        self._finishing: dict[str, OpaqueExchange] = {}
        self._in_flight = 0
        self._closed = False
        self._cleanup_failed = False

    @property
    def active_verifiers(self):
        with self._lock:
            return self._in_flight

    @property
    def admission(self) -> BrowserAdmission:
        """Session registry for trusted callers after verified app handoff."""
        return self._admission

    def _matches(self, binding):
        return type(binding) is WebBinding and binding == self._binding

    @property
    def cleanup_failed(self):
        with self._lock:
            return self._cleanup_failed

    def _discard(self, exchange):
        try:
            exchange.discard()
        except Exception:  # noqa: BLE001 - never grant access after failed verifier cleanup.
            with self._lock:
                self._cleanup_failed = True
            self._admission.close()
            return False
        return True

    def _release(self, ident, exchange):
        try:
            return self._discard(exchange)
        finally:
            # Keep the slot occupied while cleanup is in progress.
            with self._lock:
                self._entries.pop(ident, None)

    def _expire_waiting(self):
        retired = []
        with self._lock:
            now = monotonic()
            for ident, entry in self._entries.items():
                if entry is not None and entry.expires_at <= now:
                    self._entries[ident] = None
                    retired.append((ident, entry.exchange))
        for ident, exchange in retired:
            self._release(ident, exchange)

    def expire(self) -> None:
        self._expire_waiting()
        self._admission.expire()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            exchanges = [
                entry.exchange for entry in self._entries.values() if entry is not None
            ] + list(self._finishing.values())
            self._entries.clear()
            self._registrations.clear()
            self._setup = b""
        self._admission.close()
        for exchange in exchanges:
            self._discard(exchange)

    def start(
        self, binding: WebBinding, *, browser_nonce: str, username: str, ke1: str
    ) -> PakeChallenge | None:
        """Reserve an exchange slot and return KE2 (real or dummy), not a grant."""
        if not self._matches(binding) or not _token(browser_nonce):
            return None
        try:
            username = web_accounts._username(username)
        except web_accounts.WebAccountsError:
            return None
        request = _decode(ke1, self._sizes.ke1)
        if request is None:
            return None
        self._expire_waiting()
        with self._lock:
            now = monotonic()
            if (
                self._closed
                or self._cleanup_failed
                or len(self._entries) >= self._max_challenges
                or self._in_flight >= self._max_in_flight
            ):
                return None
            try:
                ident = secrets.token_urlsafe(_TOKEN_BYTES)
            except OSError:
                return None
            if ident in self._entries:
                return None
            self._entries[ident] = None
            self._in_flight += 1
            expires_at = now + self._ttl
            setup = self._setup
            registration = self._registrations.get(username)
            context = _context(self._auth_origin, binding.origin, ident)
        published = False
        exchange = None
        try:
            if not self._admission.try_attempt(binding):
                return None
            try:
                result = self._backend.start(
                    setup=setup,
                    credential_identifier=username.encode("ascii"),
                    context=context,
                    registration=registration,
                    ke1=request,
                    deadline=expires_at,
                )
            except Exception:  # noqa: BLE001 - an adapter error is a uniform sign-in refusal.
                return None
            if type(result) is not tuple or len(result) != 2:
                return None
            response, candidate = result
            if callable(getattr(candidate, "discard", None)):
                exchange = candidate
            if (
                exchange is None
                or not callable(getattr(exchange, "finish", None))
                or type(response) is not bytes
                or len(response) != self._sizes.ke2
            ):
                return None
            challenge = PakeChallenge(ident, _b64(response), _b64(context))
            with self._lock:
                if self._closed or self._cleanup_failed or expires_at <= monotonic():
                    return None
                self._entries[ident] = _Challenge(
                    expires_at,
                    browser_nonce,
                    username if registration is not None else None,
                    exchange,
                )
                published = True
            return challenge
        finally:
            if not published and exchange is not None:
                self._discard(exchange)
            with self._lock:
                if not published:
                    self._entries.pop(ident, None)
                self._in_flight -= 1

    def finish(
        self, binding: WebBinding, *, browser_nonce: str, challenge_id: str, ke3: str
    ) -> str | None:
        """Consume the proof and return a verified principal to a trusted caller."""
        if (
            not self._matches(binding)
            or not _token(browser_nonce)
            or not _token(challenge_id)
        ):
            return None
        refusal = False
        with self._lock:
            entry = self._entries.get(challenge_id)
            if self._closed or self._cleanup_failed or entry is None:
                return None
            if entry.expires_at <= monotonic():
                refusal = True
            elif not secrets.compare_digest(entry.browser_nonce, browser_nonce):
                return None
            # The rightful browser gets one finish attempt even for malformed
            # proof or verifier saturation. Wrong browsers cannot burn it.
            if self._in_flight >= self._max_in_flight:
                refusal = True
            self._entries[challenge_id] = None
            if not refusal:
                self._finishing[challenge_id] = entry.exchange
                self._in_flight += 1
        if refusal:
            self._release(challenge_id, entry.exchange)
            return None
        try:
            proof = _decode(ke3, self._sizes.ke3)
            verified = False
            try:
                if proof is not None:
                    verified = entry.exchange.finish(proof)
            except Exception:  # noqa: BLE001 - do not expose verifier-specific errors.
                verified = False
            finally:
                cleaned = self._discard(entry.exchange)
            if not cleaned or verified is not True or entry.principal is None:
                return None
            with self._lock:
                if (
                    self._closed
                    or self._cleanup_failed
                    or entry.expires_at <= monotonic()
                ):
                    return None
                return entry.principal
        finally:
            with self._lock:
                self._entries.pop(challenge_id, None)
                self._finishing.pop(challenge_id, None)
                self._in_flight -= 1
