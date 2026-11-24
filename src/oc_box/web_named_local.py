"""Internal loopback sign-in composition; not a listener or browser API.

The trusted caller must first check exact SNI/Host/Origin, TLS, verified private
owner lease and the browser's paired account/app bootstrap. Neither a request
body field nor a browser-supplied workspace selects any of those values.
"""

from threading import BoundedSemaphore, Event

if __package__:
    from . import (
        web_admission,
        web_handoff,
        web_local_origins,
        web_native_pair,
        web_pake_flow,
    )
else:
    import web_admission
    import web_handoff
    import web_local_origins
    import web_native_pair
    import web_pake_flow


class LocalNamedSignIn:
    """Coordinate PAKE, one-use handoff and native cookie without returning it."""

    def __init__(self, *, origins, binding, flow, owner_current, pair_native):
        if (
            type(origins) is not web_local_origins.LocalOrigins
            or type(binding) is not web_admission.WebBinding
            or binding.origin != origins.workspace.value
            or type(flow) is not web_pake_flow.PakeFlow
            or not flow.matches(binding, origins.account.value)
            or not callable(owner_current)
            or not callable(pair_native)
        ):
            raise ValueError("Invalid local named sign-in composition.")
        self._binding = binding
        self._workspace_origin = origins.workspace
        self._auth_origin = origins.account.value
        self._flow = flow
        self._handoff = web_handoff.WebHandoff(binding, auth_origin=self._auth_origin)
        self._owner_current = owner_current
        self._pair_native = pair_native
        self._pairs = BoundedSemaphore(2)
        self._stopped = Event()

    @property
    def admission(self):
        return self._flow.admission

    def _live(self):
        if self._stopped.is_set():
            return False
        if self._flow.cleanup_failed:
            self.close()
            return False
        try:
            if self._owner_current(self._binding) is True:
                return not self._stopped.is_set()
        except Exception:  # noqa: BLE001 - never expose an owner discovery failure.
            self._stopped.set()
        self.close()
        return False

    def start(self, *, browser_nonce, username, ke1):
        if not self._live():
            return None
        challenge = self._flow.start(
            self._binding, browser_nonce=browser_nonce, username=username, ke1=ke1
        )
        return challenge if challenge is not None and self._live() else None

    def finish(self, *, browser_nonce, challenge_id, ke3):
        if not self._live():
            return None
        principal = self._flow.finish(
            self._binding,
            browser_nonce=browser_nonce,
            challenge_id=challenge_id,
            ke3=ke3,
        )
        if principal is None or not self._live():
            return None
        return self._handoff.issue(
            self._binding,
            auth_origin=self._auth_origin,
            browser_nonce=browser_nonce,
            principal=principal,
        )

    def redeem(self, ticket, *, browser_nonce, claim_browser):
        """Only same-browser confirmation can reach native pairing.

        The trusted claim consumes this browser's bootstrap once and returns a
        liveness check. Return only the opaque app lease; keep the native
        credential in admission state for the eventual relay.
        """
        if (
            not callable(claim_browser)
            or not self._live()
            or not self._pairs.acquire(blocking=False)
        ):
            return None
        try:
            principal = self._handoff.consume(
                self._binding,
                ticket,
                auth_origin=self._auth_origin,
                browser_nonce=browser_nonce,
            )
            if principal is None or not self._live():
                return None
            current = claim_browser(browser_nonce)
            if not callable(current) or current() is not True:
                return None
            try:
                native = self._pair_native(self._binding, self._workspace_origin)
            except web_native_pair.NativePairError:
                return None
            if (
                type(native) is not web_native_pair.NativeCookie
                or not self._live()
                or current() is not True
            ):
                return None
            try:
                token = self.admission.issue(
                    self._binding, principal=principal, native_cookie=native
                )
            except OSError:
                return None
            if token is None:
                return None
            publish = False
            try:
                # The final check linearizes successful bootstrap confirmation.
                # Until then the just-minted token must not leave this method.
                publish = self._live() and current() is True
                return token if publish else None
            finally:
                if not publish:
                    session = self.admission.lookup(self._binding, token)
                    if session is not None:
                        self.admission.revoke(self._binding, session)
        finally:
            self._pairs.release()

    def handoff_available(self, ticket):
        """Cross-site document gate only: this does not authenticate the browser."""
        return (
            self._live()
            and self._handoff.available(
                self._binding, ticket, auth_origin=self._auth_origin
            )
            and self._live()
        )

    def expire(self):
        self._handoff.expire()
        self._flow.expire()

    def close(self):
        """Revoke pending and active grants; caller must wait for workers to drain."""
        self._stopped.set()
        self._handoff.close()
        self._flow.close()
