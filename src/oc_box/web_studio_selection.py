"""Unserved server-side Studio selection state for one named owner lifetime."""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hmac
import os
import re
import secrets
from threading import Lock
from time import monotonic

if __package__:
    from . import web_admission, web_named_owner
    from . import web_studio_conversation as studio
    from .web_studio_read import ReadScope
else:
    import web_admission
    import web_named_owner
    import web_studio_conversation as studio
    from web_studio_read import ReadScope


_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_ORDINAL = 2**64 - 1
_MAX_GENERATION = 2**53 - 1


class SelectionError(ValueError):
    """Fixed refusal; no page, grant, session or verifier diagnostics."""

    def __init__(self):
        super().__init__("The Studio page selection could not be verified.")


def _refuse():
    raise SelectionError() from None


def _nonce(value):
    return (
        type(value) is str and len(value) == 43 and _NONCE.fullmatch(value) is not None
    )


def _session(value):
    return type(value) is str and bool(studio._session_id(value))


def _deadline(value):
    now = monotonic()
    if type(value) not in (int, float) or not now < value < float("inf"):
        _refuse()
    return min(value, now + studio.HOST_TIMEOUT)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class PageSnapshot:
    """Exact server view. Only public_state() is intended for serialization."""

    nonce: str
    generation: int
    _browser: web_admission.BrowserSession
    scope: ReadScope | None

    @property
    def session_id(self):
        return None if self.scope is None else self.scope.session_id

    def public_state(self):
        return {
            "page": self.nonce,
            "generation": self.generation,
            "sessionID": self.session_id,
        }


@dataclass(slots=True, repr=False)
class _Page:
    browser: web_admission.BrowserSession
    expires_at: float
    sequence: int = 0
    generation: int = 0
    view: PageSnapshot | None = None  # None means unset while verifying or refused.


class SelectionRegistry:
    """One exact BrowserAdmission and owner generation, with no route of its own.

    The producer holds the original verified owner lease. ``observe_owner``
    must synchronously recheck that owner (not discover or adopt another),
    honor the absolute deadline, and return its exact OwnerIdentity or None.
    All operations take actual preauthenticated grant snapshots, never tokens
    from a request. The caller still owns grant-use registration and drain.
    """

    def __init__(
        self, owner, admission, binding, *, observe_owner, max_pages=128, page_ttl=1800
    ):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(owner) is not web_named_owner.OwnerIdentity
            or owner.version != studio.NATIVE_VERSION
            or type(admission) is not web_admission.BrowserAdmission
            or not admission._matches(binding)
            or binding.workspace_id != owner.workspace_id
            or binding.owner_generation != owner.generation
            or not callable(observe_owner)
            or type(max_pages) is not int
            or not 1 <= max_pages <= 1024
            or type(page_ttl) not in (int, float)
            or not 0 < page_ttl <= 3600
        ):
            _refuse()
        self._owner = owner
        self._admission = admission
        self._observe_owner = observe_owner
        self._max_pages = max_pages
        self._page_ttl = page_ttl
        self._lock = Lock()
        self._closed = False
        self._nonce_key = secrets.token_bytes(32)
        self._ordinal = 0
        self._pages = {}

    def close(self):
        """Permanently fence these pages, including in-flight observations."""
        with self._lock:
            self._closed = True
            self._pages.clear()

    def _require_owner(self, deadline):
        with self._lock:
            if self._closed or monotonic() >= deadline:
                _refuse()
        owner = None
        try:
            owner = self._observe_owner(deadline)
        except Exception:  # Keep private owner diagnostics out of responses.
            pass
        if type(owner) is not web_named_owner.OwnerIdentity or owner != self._owner:
            self.close()
            _refuse()

    @contextmanager
    def _live(self, deadline):
        # Check actual grant identity under its lock, without lookup's
        # out-of-lock cancellation callbacks. Lock order: admission, registry.
        # Never call owner/verifier code or signal transports under either lock.
        with self._admission._lock, self._lock:
            if self._admission._closed:
                self._closed = True
                self._pages.clear()
            now = monotonic()
            if self._closed or now >= deadline:
                _refuse()
            yield now

    def _granted(self, browser, now):
        return (
            type(browser) is web_admission.BrowserSession
            and type(browser._token) is str
            and self._admission._entry(browser) is not None
            and now < browser.expires_at
        )

    def _sweep(self, now):
        for nonce, page in tuple(self._pages.items()):
            if now >= page.expires_at or not self._granted(page.browser, now):
                del self._pages[nonce]

    def _page(self, browser, nonce, now):
        page = self._pages.get(nonce)
        if (
            page is None
            or page.browser is not browser
            or now >= page.expires_at
            or not self._granted(browser, now)
        ):
            _refuse()
        return page

    def bootstrap(self, browser, *, deadline):
        """Issue one new unselected page, only for authenticated first-party HTML."""
        deadline = _deadline(deadline)
        self._require_owner(deadline)
        try:
            entropy = secrets.token_bytes(24)
        except OSError:
            _refuse()
        with self._live(deadline) as now:
            self._sweep(now)
            if (
                not self._granted(browser, now)
                or len(self._pages) >= self._max_pages
                or self._ordinal >= _MAX_ORDINAL
            ):
                _refuse()
            self._ordinal += 1
            # Never reuse a nonce: different entropy gives a different
            # prefix; repeating entropy gives the same mask but a distinct
            # ordinal. Mask the ordinal so it doesn't expose issuance counts.
            mask = int.from_bytes(
                hmac.digest(self._nonce_key, entropy, "sha256")[:8], "big"
            )
            opaque = (self._ordinal ^ mask).to_bytes(8, "big")
            nonce = (
                base64.urlsafe_b64encode(entropy + opaque).rstrip(b"=").decode("ascii")
            )
            view = PageSnapshot(nonce, 0, browser, None)
            self._pages[nonce] = _Page(
                browser, min(now + self._page_ttl, browser.expires_at), view=view
            )
            return view

    def change(self, browser, nonce, *, sequence, session_id, deadline, verify=None):
        """Apply one newer page intent; independently verify its exact session.

        ``verify(owner, browser, session_id, deadline)`` must independently
        authorize that exact workspace selection and return the exact ID.
        It runs outside both locks. ``None`` as the requested ID means Home;
        there is no session to authorize. Sequence only orders requests.
        """
        if (
            not _nonce(nonce)
            or type(sequence) is not int
            or not 1 <= sequence <= _MAX_GENERATION
            or (
                session_id is not None
                and (not _session(session_id) or not callable(verify))
            )
        ):
            _refuse()
        deadline = _deadline(deadline)
        self._require_owner(deadline)
        with self._live(deadline) as now:
            page = self._page(browser, nonce, now)
            if sequence <= page.sequence:
                _refuse()
            page.sequence = sequence
            page.generation += 1  # At most the bounded sequence high-water.
            generation = page.generation
            page.view = (
                None  # Invalidate the old view before asynchronous verification.
            )
            deadline = min(deadline, page.expires_at)
            if session_id is None:
                page.view = PageSnapshot(nonce, generation, browser, None)
                return page.view

        approved = None
        try:
            approved = verify(self._owner, browser, session_id, deadline)
        except Exception:  # No verifier diagnostics cross this boundary.
            pass
        if type(approved) is not str or approved != session_id:
            _refuse()
        self._require_owner(deadline)
        with self._live(deadline) as now:
            current = self._page(browser, nonce, now)
            if current is not page or current.generation != generation:
                _refuse()
            scope = ReadScope(self._owner, browser, approved, generation)
            page.view = PageSnapshot(nonce, generation, browser, scope)
            return page.view

    def snapshot(self, browser, nonce, *, session_id, generation, deadline):
        """Match a GET's assertions to an already approved exact page view."""
        if (
            not _nonce(nonce)
            or not _session(session_id)
            or type(generation) is not int
            or not 1 <= generation <= _MAX_GENERATION
        ):
            _refuse()
        deadline = _deadline(deadline)
        self._require_owner(deadline)
        with self._live(deadline) as now:
            view = self._page(browser, nonce, now).view
            if (
                view is None
                or view.generation != generation
                or view.session_id != session_id
            ):
                _refuse()
            return view

    def observe(self, view, deadline):
        """Recheck the original view, never substitute a newer one or another page."""
        if type(view) is not PageSnapshot or not _nonce(view.nonce):
            _refuse()
        deadline = _deadline(deadline)
        self._require_owner(deadline)
        with self._live(deadline) as now:
            if self._page(view._browser, view.nonce, now).view is not view:
                _refuse()
            return view.scope

    def retire(self, browser, nonce, *, deadline):
        """Retire only the authenticated page, not another page or its grant."""
        if not _nonce(nonce):
            _refuse()
        deadline = _deadline(deadline)
        self._require_owner(deadline)
        with self._live(deadline) as now:
            self._page(browser, nonce, now)
            del self._pages[nonce]

    def expire(self, *, deadline):
        """Sweep expired or revoked pages from the existing owner timer."""
        deadline = _deadline(deadline)
        self._require_owner(deadline)
        with self._live(deadline) as now:
            self._sweep(now)
