"""Verify the guest cookie on counted v3 tunnels, not on a legacy raw relay."""

from threading import Event, current_thread
from time import monotonic

if __package__:
    from . import (
        web_guest_duplex_exec,
        web_guest_worker_drain,
        web_studio_guest,
        web_studio_guest_binding,
        web_studio_guest_native as native,
    )
else:
    import web_guest_duplex_exec
    import web_guest_worker_drain
    import web_studio_guest
    import web_studio_guest_binding
    import web_studio_guest_native as native


class DuplexGuestCookieVerifier:
    """Single exact accepted-worker attempt; both relays must retire cleanly.

    The route owner must register the guest use before invoking this object
    and join its cancellation to ``cancel``. It must still account for the
    separately counted browser exchange. No credentials enter argv or env.
    """

    def __init__(self, binding, workers, worker_use):
        if (
            type(binding) is not web_studio_guest_binding._UnservedBinding
            or type(workers) is not web_guest_worker_drain.AcceptedWorkers
            or not workers.current(worker_use)
        ):
            native._refuse()
        self._binding = binding
        self._workers = workers
        self._worker_use = worker_use
        self._thread = current_thread()
        self._cancelled = Event()
        self._started = False
        self._uncertain = None

    @property
    def cleanup_confirmed(self):
        return self._uncertain is None

    def cancel(self):
        self._cancelled.set()

    def __call__(self, binding, *, use, headers, authority, deadline):
        if current_thread() is not self._thread or self._started:
            return None
        self._started = True
        original = self._binding
        scope = original.native
        if (
            binding is not scope.binding
            or type(use) is not web_studio_guest.GuestUse
            or type(deadline) not in (int, float)
            or not monotonic() < deadline <= use.deadline < float("inf")
        ):
            return None
        limit = min(deadline, monotonic() + native._SECONDS)

        def cheap():
            left = limit - monotonic()
            if (
                left <= 0
                or use.cancelled
                or self._cancelled.is_set()
                or not self._workers.current(self._worker_use)
            ):
                native._refuse()
            return left

        def current():
            cheap()
            if original.current() is not binding:
                native._refuse()
            cheap()

        try:
            cookie = native._cookie(binding, authority, headers)
            current()
            for value in (None, cookie):
                reply = self._exchange(native._wire(binding, value), cheap, limit)
                native._reply(reply, scope, authenticated=value is not None)
                current()
            return binding
        except Exception:
            return None

    def _exchange(self, request, check, deadline):
        relay = web_guest_duplex_exec.GuestDuplexExec(
            self._binding, self._workers, self._worker_use
        )
        self._uncertain = relay
        try:
            peer = relay.start(deadline=deadline)

            def step():
                peer.advance(milliseconds=min(25, int(check() * 1000)))

            check()
            peer.open()
            while not peer.ready:
                step()
            check()
            if not peer.send(request):
                native._refuse()
            reply = bytearray()
            while True:
                check()
                frame = peer.receive()
                if frame is not None:
                    if len(frame) > native._MAX_RESPONSE - len(reply):
                        native._refuse()
                    reply.extend(frame)
                    peer.consumed()
                elif peer.ended:
                    break
                step()
            return bytes(reply)
        finally:
            # Only the exact v3 cancellation receipt, EOF and reap can
            # discharge an attempted exec, even after an apparent native 401/200.
            if relay.retire() is not True:
                native._refuse()
            self._uncertain = None
