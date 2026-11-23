"""Memory-only start of a local account/app browser handshake.

Trusted host caller chooses the origins and verifies TLS and request context.
The workspace origin produces the app nonce in an HttpOnly cookie and a
one-use ticket in a protected POST. Redeeming the ticket on the account site
produces a separate account nonce for its HttpOnly cookie. Server-side
translation supplies the original app nonce to the sign-in coordinator;
neither hostname reads the other one's cookies. Never log any value here.
"""

import re
import secrets
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic

if __package__:
    from .web_admission import WebBinding
    from .web_local_origins import LocalOrigins
else:
    from web_admission import WebBinding
    from web_local_origins import LocalOrigins

_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")


@dataclass(frozen=True, slots=True, repr=False)
class BootstrapTicket:
    value: str
    app_nonce: str


@dataclass(frozen=True, slots=True, repr=False)
class _Exchange:
    app_nonce: str
    expiry: float


def _digest(value):
    return sha256(value.encode("ascii")).digest()


def _valid(value):
    return (
        type(value) is str and len(value) == 43 and _TOKEN.fullmatch(value) is not None
    )


class BrowserBootstrap:
    """Owned by one live loopback owner; bounded and closed with that owner."""

    def __init__(self, *, origins, binding, ttl=60, capacity=32):
        if (
            type(origins) is not LocalOrigins
            or type(binding) is not WebBinding
            or binding.origin != origins.workspace.value
            or type(ttl) not in (int, float)
            or not 0 < ttl <= 120
            or type(capacity) is not int
            or not 0 < capacity <= 128
        ):
            raise ValueError("Invalid local browser bootstrap configuration.")
        self._binding = binding
        self._auth_origin = origins.account.value
        self._ttl = ttl
        self._capacity = capacity
        self._waiting: dict[bytes, _Exchange] = {}
        self._accounts: dict[bytes, _Exchange] = {}
        self._issued_app_nonces: dict[bytes, float] = {}
        self._issued_account_nonces: dict[bytes, float] = {}
        self._issued_tickets: dict[bytes, float] = {}
        self._closed = False
        self._lock = Lock()

    def _matches(self, binding, auth_origin):
        return (
            type(binding) is WebBinding
            and binding == self._binding
            and auth_origin == self._auth_origin
        )

    def _expire(self, now):
        for table in (self._waiting, self._accounts):
            for key, entry in tuple(table.items()):
                if entry.expiry <= now:
                    del table[key]
        for ledger in (
            self._issued_app_nonces,
            self._issued_account_nonces,
            self._issued_tickets,
        ):
            for key, expiry in tuple(ledger.items()):
                if expiry <= now:
                    del ledger[key]

    def issue(self, binding):
        if type(binding) is not WebBinding or binding != self._binding:
            return None
        try:
            ticket, app_nonce = (secrets.token_urlsafe(32) for _ in range(2))
        except OSError:
            return None
        key, app_key = _digest(ticket), _digest(app_nonce)
        with self._lock:
            now = monotonic()
            self._expire(now)
            if (
                self._closed
                or len(self._waiting) + len(self._accounts) >= self._capacity
                or key in self._issued_tickets
                or app_key in self._issued_app_nonces
                or len(self._issued_app_nonces) >= 2 * self._capacity
                or app_nonce == ticket
            ):
                return None
            self._waiting[key] = _Exchange(app_nonce, now + self._ttl)
            self._issued_app_nonces[app_key] = now + self._ttl
            self._issued_tickets[key] = now + self._ttl
            return BootstrapTicket(ticket, app_nonce)

    def accept(self, binding, *, auth_origin, ticket):
        """One-use account entry only. Not an app grant or authorization."""
        if not self._matches(binding, auth_origin) or not _valid(ticket):
            return None
        with self._lock:
            self._expire(monotonic())
            entry = self._waiting.pop(_digest(ticket), None)
            if self._closed or entry is None:
                return None
            try:
                nonce = secrets.token_urlsafe(32)
            except OSError:
                return None
            key = _digest(nonce)
            if (
                key in self._issued_account_nonces
                or len(self._issued_account_nonces) >= 2 * self._capacity
                or nonce in (ticket, entry.app_nonce)
            ):
                return None
            self._accounts[key] = entry
            self._issued_account_nonces[key] = entry.expiry
            return nonce

    def app_nonce(self, binding, *, auth_origin, account_nonce):
        """Translate a trusted account cookie without disclosing it to the app."""
        if not self._matches(binding, auth_origin) or not _valid(account_nonce):
            return None
        with self._lock:
            self._expire(monotonic())
            entry = self._accounts.get(_digest(account_nonce))
            return entry.app_nonce if entry and not self._closed else None

    def retire(self, binding, *, auth_origin, account_nonce):
        if not self._matches(binding, auth_origin) or not _valid(account_nonce):
            return False
        with self._lock:
            self._expire(monotonic())
            return self._accounts.pop(_digest(account_nonce), None) is not None

    def paired_app_current(self, binding, app_nonce):
        """Check that an app-side cookie belongs to an accepted live bootstrap."""
        if (
            type(binding) is not WebBinding
            or binding != self._binding
            or not _valid(app_nonce)
        ):
            return False
        with self._lock:
            self._expire(monotonic())
            return not self._closed and any(
                secrets.compare_digest(entry.app_nonce, app_nonce)
                for entry in self._accounts.values()
            )

    def expire(self):
        with self._lock:
            self._expire(monotonic())

    def close(self):
        with self._lock:
            self._closed = True
            self._waiting.clear()
            self._accounts.clear()
            self._issued_app_nonces.clear()
            self._issued_account_nonces.clear()
            self._issued_tickets.clear()
