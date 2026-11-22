"""Private, one-use handoff from named sign-in to one HTTPS workspace app.

Trusted host callers only. Construct this for one verified workspace/owner
generation and one configured, separate host-controlled authentication origin.
Before issuing, verify the live owner and authenticate and authorize the named
principal for that workspace. Before consuming, reverify the live owner. Supply
``auth_origin`` from the verified exchange, not an unvalidated request field.
The caller verifies HTTPS, the app and auth origin policies, and possession of
the app-side browser bootstrap. Independently mint a fresh nonce per app
bootstrap with ``secrets.token_urlsafe(32)``. Bind it to that browser and
exchange; never accept it as proof from the token-carrier body or URL.

This class performs no browser, cookie, credential, or upstream I/O. Distinct
hostnames are required, but do not establish cookie isolation or a trustworthy
deployment. The caller also owns request size/rate limits and uniform refusal
responses. Never put raw tokens, nonce values, or protocol data in URLs or logs.
"""

import base64
import re
import secrets
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic

if __package__:
    from .web_admission import WebBinding as _WebBinding
    from .web_tls import parse_https_origin as _parse_https_origin
else:
    from web_admission import WebBinding as _WebBinding
    from web_tls import parse_https_origin as _parse_https_origin

_RANDOM_BYTES = 32
_SERIAL_BYTES = 8
_MAX_SERIAL = (1 << (8 * _SERIAL_BYTES)) - 1
# Canonical unpadded base64url, including zero trailing pad bits.
_NONCE = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{53}[AQgw]")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class HandoffToken:
    """Opaque private value; unwrap via ``value`` only for protected transport."""

    value: str


@dataclass(frozen=True, slots=True, repr=False)
class _Pending:
    principal: str
    nonce_digest: bytes
    expires_at: float


def _valid(value, length, pattern):
    return (
        type(value) is str
        and len(value) == length
        and pattern.fullmatch(value) is not None
    )


def _digest(value):
    return sha256(value.encode("ascii")).digest()


class WebHandoff:
    """Bounded pending grants; request methods refuse with None or False.

    A matching consume deletes the grant before returning the principal. Use
    that result for at most one immediate app-admission attempt (for example
    ``BrowserAdmission.issue``); never retry or enqueue it if admission fails.
    Revocation, expiry and close affect only pending grants, not a successful
    consume or later sessions. The caller must coordinate closing this table
    and fencing any downstream admission on owner or authorization changes.
    Call ``expire()`` from a lifecycle timer to discard expired idle state.
    """

    def __init__(
        self,
        binding: _WebBinding,
        *,
        auth_origin: str,
        ttl: float = 30,
        max_pending: int = 64,
    ):
        invalid = "Invalid web handoff configuration."
        if (
            type(binding) is not _WebBinding
            or type(auth_origin) is not str
            or type(ttl) not in (int, float)
            or not 0 < ttl <= 60
            or type(max_pending) is not int
            or not 0 < max_pending <= 256
        ):
            raise ValueError(invalid)
        try:
            auth = _parse_https_origin(auth_origin)
        except ValueError:
            raise ValueError(invalid) from None
        if (
            auth.value != auth_origin
            or auth.hostname == _parse_https_origin(binding.origin).hostname
        ):
            raise ValueError(invalid)
        self._binding = binding
        self._auth_origin = auth_origin
        self._ttl = ttl
        self._max_pending = max_pending
        self._lock = Lock()
        self._pending: dict[bytes, _Pending] = {}
        self._serial = 0
        self._closed = False

    def _matches(self, binding, auth_origin, browser_nonce):
        return (
            type(binding) is _WebBinding
            and binding == self._binding
            and type(auth_origin) is str
            and auth_origin == self._auth_origin
            and _valid(browser_nonce, 43, _NONCE)
        )

    def _expire(self, now):
        # Caller holds the lock; the scan is bounded by max_pending.
        for key, pending in tuple(self._pending.items()):
            if pending.expires_at <= now:
                del self._pending[key]

    def issue(
        self,
        binding: _WebBinding,
        *,
        auth_origin: str,
        browser_nonce: str,
        principal: str,
    ) -> HandoffToken | None:
        """Mint after independent authentication and workspace authorization."""
        if (
            not self._matches(binding, auth_origin, browser_nonce)
            or type(principal) is not str
            or not 0 < len(principal) <= 128
            or not principal.isprintable()
        ):
            return None
        with self._lock:
            if self._closed or self._serial == _MAX_SERIAL:
                return None
            self._expire(monotonic())
            if len(self._pending) >= self._max_pending:
                return None
            try:
                random = secrets.token_bytes(_RANDOM_BYTES)
            except OSError:
                return None
            # A nonsecret, fixed-width serial prevents token reuse within this
            # lifetime even if randomness repeats after a grant is retired.
            self._serial += 1
            raw = self._serial.to_bytes(_SERIAL_BYTES, "big") + random
            value = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            key = _digest(value)
            if key in self._pending:
                return None
            self._pending[key] = _Pending(
                principal, _digest(browser_nonce), monotonic() + self._ttl
            )
            return HandoffToken(value)

    def _take(self, binding, token, auth_origin, browser_nonce):
        if not self._matches(binding, auth_origin, browser_nonce) or not _valid(
            token, 54, _TOKEN
        ):
            return None
        key = _digest(token)
        nonce_digest = _digest(browser_nonce)
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:  # close clears the table under the same lock.
                return None
            if pending.expires_at <= monotonic():
                del self._pending[key]
                return None
            if not secrets.compare_digest(pending.nonce_digest, nonce_digest):
                return None
            return self._pending.pop(key)

    def consume(
        self, binding: _WebBinding, token: str, *, auth_origin: str, browser_nonce: str
    ) -> str | None:
        """Take one matching grant; a binding mismatch does not burn it."""
        pending = self._take(binding, token, auth_origin, browser_nonce)
        return pending.principal if pending is not None else None

    def revoke(
        self, binding: _WebBinding, token: str, *, auth_origin: str, browser_nonce: str
    ) -> bool:
        """Discard exactly one matching pending handoff without admitting it."""
        return self._take(binding, token, auth_origin, browser_nonce) is not None

    def expire(self) -> None:
        """Discard expired grants, including when there is no request traffic."""
        with self._lock:
            self._expire(monotonic())

    def close(self) -> None:
        """Revoke all pending grants and permanently stop issuance."""
        with self._lock:
            self._closed = True
            self._pending.clear()
