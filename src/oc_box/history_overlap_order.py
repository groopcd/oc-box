"""Pass-local ordering for one checked source read ahead of serial host work."""

from dataclasses import dataclass
from itertools import islice
import re


class OverlapRefusal(ValueError):
    """Fixed refusal; bindings and transcript data remain private."""

    def __init__(self, category, *, cleanup_failed=False):
        super().__init__(f"History read-ahead refused ({category}).")
        self.history_contract_refusal = True
        self.history_source_cleanup_failed = cleanup_failed


@dataclass(frozen=True)
class Route:
    source: str
    archive: str


def _session(value):
    return (
        type(value) is str
        and re.fullmatch(r"ses[A-Za-z0-9_-]{1,253}", value) is not None
    )


class OrderedOverlap:
    """Own one speculative read and never schedule destination operations.

    The caller retains the original workspace lock and rechecks admission
    at every spawn; this class does not copy launcher context to a worker.
    ``begin`` must either return an owned handle or propagate a refusal
    with honest cleanup. ``receive`` must prove cleanup before returning
    or raising the pending exception. ``cancel`` must independently prove
    retirement; a successful wrapper exit alone is insufficient.
    """

    def __init__(self, routes, *, read, begin, receive, cancel, pending_error):
        routes = tuple(islice(routes, 10001))
        if (
            len(routes) > 10000
            or any(
                type(route) is not Route
                or not _session(route.source)
                or not _session(route.archive)
                for route in routes
            )
            or len({route.source for route in routes}) != len(routes)
            or len({route.archive for route in routes}) != len(routes)
        ):
            raise OverlapRefusal("inventory-binding")
        self._routes = routes
        self._read = read
        self._begin = begin
        self._receive = receive
        self._cancel = cancel
        self._pending_error = pending_error
        self._index = 0
        self._slot = None
        self._active = False
        self._failed = False
        self._current_can_overlap = False

    def __enter__(self):
        if self._active or self._failed:
            raise OverlapRefusal("pass-reuse")
        self._active = True
        return self

    def _require_active(self):
        if not self._active or self._failed:
            raise OverlapRefusal("pass-ended")

    def take(self, route):
        self._require_active()
        if self._routes[self._index : self._index + 1] != (route,):
            self._failed = True
            raise OverlapRefusal("inventory-order")
        self._current_can_overlap = False
        try:
            if self._slot is None:
                value = self._read(route)
            else:
                pending_route, handle = self._slot
                if route != pending_route:
                    raise OverlapRefusal("inventory-order")
                value = self._receive(pending_route, handle)
        except self._pending_error:
            self._slot = None
            self._index += 1
            raise
        except BaseException:
            self._failed = True
            raise
        self._slot = None
        self._index += 1
        self._current_can_overlap = True
        return value

    def before_serial_destination(self):
        """Speculate only when the current verified source needs host work."""
        self._require_active()
        if not self._current_can_overlap or self._slot is not None:
            self._failed = True
            raise OverlapRefusal("read-ownership")
        self._current_can_overlap = False
        if self._index == len(self._routes):
            return
        route = self._routes[self._index]
        try:
            handle = self._begin(route)
            if handle is None:
                raise OverlapRefusal("read-ownership", cleanup_failed=True)
            self._slot = route, handle
        except BaseException:
            self._failed = True
            raise

    def __exit__(self, kind, error, _traceback):
        failed = self._failed
        self._active = False
        self._failed = True
        slot, self._slot = self._slot, None
        if slot is not None:
            try:
                if self._cancel(*slot) is not True:
                    raise OverlapRefusal("cleanup-unverified", cleanup_failed=True)
            except (KeyboardInterrupt, SystemExit) as cleanup:
                interruption = (
                    error
                    if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else cleanup
                )
                interruption.history_source_cleanup_failed = True
                if isinstance(interruption, SystemExit) and (
                    interruption.code is None or interruption.code == 0
                ):
                    interruption.code = 1
                if interruption is cleanup:
                    raise
            except BaseException:
                if error is None:
                    raise OverlapRefusal(
                        "cleanup-unverified", cleanup_failed=True
                    ) from None
                error.history_source_cleanup_failed = True
        if kind is None and (failed or self._index != len(self._routes)):
            raise OverlapRefusal("incomplete-pass")
