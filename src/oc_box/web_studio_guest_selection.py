"""Exact one-use Studio page selections, independent of named HTTPS grants."""

from contextlib import contextmanager
from dataclasses import dataclass
import re
from threading import Lock
from time import monotonic

if __package__:
    from . import web_studio_guest
else:
    import web_studio_guest


_SESSION = re.compile(r"ses[A-Za-z0-9_-]{1,125}\Z")
_MAX_GENERATION = (1 << 53) - 1


class GuestSelectionError(ValueError):
    def __init__(self):
        super().__init__("The Studio guest page selection could not be verified.")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class GuestSelection:
    """A trusted exact view, not a browser-supplied generation assertion."""

    _grant: web_studio_guest.GuestGrant
    generation: int
    session_id: str | None

    def public_state(self):
        return {"generation": self.generation, "sessionID": self.session_id}


@dataclass(slots=True, repr=False)
class _Page:
    sequence: int
    generation: int
    view: GuestSelection | None


class GuestSelections:
    """Bind each view to the exact guest page grant on one gateway.

    The route must already have registered its use and authenticated the
    current native cookie. This registry neither authorizes a session on its
    own nor owns request transports. Workspace/ancestry verification must
    happen on the same held service, through the caller's registered helper.
    """

    def __init__(self, admission, binding, *, max_pages=64):
        if (
            type(admission) is not web_studio_guest.GuestAdmission
            or type(binding) is not web_studio_guest.GuestBinding
            or admission._binding is not binding
            or type(max_pages) is not int
            or not 1 <= max_pages <= 256
        ):
            raise GuestSelectionError()
        self._admission = admission
        self._binding = binding
        self._max_pages = max_pages
        self._closed = False
        self._pages = {}
        self._lock = Lock()

    def close(self):
        with self._lock:
            self._closed = True
            self._pages.clear()

    @contextmanager
    def _live(self, use):
        gate = self._admission
        if type(use) is not web_studio_guest.GuestUse or not gate.native_current(use):
            raise GuestSelectionError()
        # Lock order is admission before selection. No verifier, callbacks,
        # or native I/O while either lock is held.
        with gate._condition, self._lock:
            if self._closed or not gate._live(use):
                raise GuestSelectionError()
            entry = gate._uses[use]
            grants = set(gate._grants.values())
            grant = entry.grant
            now = monotonic()
            if (
                not entry.native_verified
                or type(grant) is not web_studio_guest.GuestGrant
                or grant not in grants
                or now >= grant.expires_at
                or now >= use.deadline
            ):
                raise GuestSelectionError()
            for stale in tuple(self._pages):
                if stale not in grants or stale.expires_at <= now:
                    del self._pages[stale]
            yield grant

    def _page(self, grant):
        page = self._pages.get(grant)
        if page is None:
            if len(self._pages) >= self._max_pages:
                raise GuestSelectionError()
            page = _Page(0, 0, GuestSelection(grant, 0, None))
            self._pages[grant] = page
        return page

    def state(self, use):
        """Return the initial Home or currently approved view, never stale."""
        with self._live(use) as grant:
            view = self._page(grant).view
            if view is None:
                raise GuestSelectionError()
            return view

    def change(self, use, *, sequence, session_id, verify=None):
        if (
            type(sequence) is not int
            or not 1 <= sequence <= _MAX_GENERATION
            or (
                session_id is not None
                and (
                    type(session_id) is not str
                    or not _SESSION.fullmatch(session_id)
                    or not callable(verify)
                )
            )
        ):
            raise GuestSelectionError()
        with self._live(use) as grant:
            page = self._page(grant)
            if sequence <= page.sequence:
                raise GuestSelectionError()
            page.sequence = sequence
            page.generation += 1
            generation = page.generation
            page.view = None
            if session_id is None:
                page.view = GuestSelection(grant, generation, None)
                return page.view

        try:
            verified = verify(self._binding, use, session_id, use.deadline)
        except Exception:
            raise GuestSelectionError() from None
        if type(verified) is not str or verified != session_id:
            raise GuestSelectionError()
        with self._live(use) as current:
            observed = self._pages.get(current)
            if (
                current is not grant
                or observed is not page
                or observed.generation != generation
            ):
                raise GuestSelectionError()
            page.view = GuestSelection(grant, generation, session_id)
            return page.view

    def snapshot(self, use, *, session_id, generation):
        if (
            type(session_id) is not str
            or not _SESSION.fullmatch(session_id)
            or type(generation) is not int
            or not 1 <= generation <= _MAX_GENERATION
        ):
            raise GuestSelectionError()
        with self._live(use) as grant:
            page = self._pages.get(grant)
            view = page.view if page is not None else None
            if (
                view is None
                or view.generation != generation
                or view.session_id != session_id
            ):
                raise GuestSelectionError()
            return view

    def observe(self, use, view):
        """Return the original exact selected view or refuse; never substitute."""
        if type(view) is not GuestSelection or view.session_id is None:
            raise GuestSelectionError()
        with self._live(use) as grant:
            page = self._pages.get(grant)
            if grant is not view._grant or page is None or page.view is not view:
                raise GuestSelectionError()
            return view
