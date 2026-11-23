"""Host-only admission for an unserved local named workspace app.

The trusted host caller holds the exact private owner lease, establishes TLS
with the dedicated app SNI policy, and passes the raw request-line method and
target and the complete header block. This is not HTTP framing, a bootstrap
issuer, a cross-site handoff receiver, or a proxy. This module creates no
listeners or upstream transports and performs no discovery or guest fallback.
Confirmation only delegates pairing to the existing trusted sign-in driver.
"""

import os

if __package__:
    from . import web_named_headers, web_named_local, web_native_pair
else:
    import web_named_headers
    import web_named_local
    import web_native_pair


CONFIRM_PATH = "/oc-box/confirm"
LOGOUT_PATH = "/oc-box/logout"
APP_COOKIE_NAME = "__Host-ocbox-app"
_BOOTSTRAP_COOKIE = "__Host-ocbox-bootstrap"

# Only two exact, read-only native metadata requests are in scope. This is
# not a native UI allowlist. In particular there are no upgrades, queries,
# path decoding, prefix rules, pairing, or native auth/credential controls.
_METADATA_PATHS = frozenset(("/api/info", "/api/session/active"))


class AppRequestError(ValueError):
    """Fixed refusal without request data or private upstream diagnostics."""

    _message = "Named workspace app request was not admitted."

    def __init__(self):
        super().__init__(self._message)


class StaleAppSessionError(AppRequestError):
    """No current session for the supplied cookie; not proof it ever existed."""

    _message = "No current named workspace app session."


class StaleAppStateError(AppRequestError):
    """The bound app/owner state is no longer usable; do not rebind or retry."""

    _message = "Named workspace app state is no longer current."


class NamedAppIngress:
    """Compose the same sign-in, browser admission and lease for one app lifetime.

    The trusted ``bootstrap_current(nonce)`` must verify an independently
    issued, still-paired account/app bootstrap for this sign-in and owner.
    It does not mint or consume a nonce.
    """

    def __init__(self, sign_in, *, endpoint, bootstrap_current):
        if os.environ.get("OC_BOX_CONTAINER") == "1":
            raise ValueError("Named workspace app admission is host-only.")
        if type(sign_in) is not web_named_local.LocalNamedSignIn or not callable(
            bootstrap_current
        ):
            raise ValueError("Expected a local named workspace sign-in.")
        # Endpoint is supplied by the trusted lease holder, never a request.
        web_native_pair._port(endpoint)
        self._endpoint = endpoint
        self._bootstrap_current = bootstrap_current
        self._sign_in = sign_in
        self._admission = sign_in.admission
        self._binding = sign_in._binding
        self._origin = sign_in._workspace_origin

    def _require_current(self):
        # Use the coordinator's actual owner callback and permanent loss
        # fence. Independently supplied browser booleans are not authority.
        if not self._sign_in._live():
            raise StaleAppStateError()

    def _cookie(self, *, method, headers, actual_tls, name):
        if type(headers) not in (list, tuple):
            raise AppRequestError()
        headers = tuple(headers[: web_named_headers._MAX_FIELDS + 1])
        try:
            token = web_named_headers.admit_headers(
                origin=self._origin,
                method=method,
                headers=headers,
                actual_tls=actual_tls,
                cookie_name=name,
            )
        except web_named_headers.NamedRequestError:
            pass
        else:
            # The shared gate permits some Origin-less safe navigations.
            # This narrower metadata profile deliberately does not.
            if any(key.lower() == "origin" for key, _ in headers):
                return token
        raise AppRequestError()

    def _session(self, token):
        self._require_current()
        session = self._admission.lookup(self._binding, token)
        if session is None:
            self._require_current()
            raise StaleAppSessionError()
        return session

    def confirm(self, *, method, target, headers, actual_tls, ticket) -> str:
        """Redeem on exact same-origin POST using only the app bootstrap cookie.

        The caller extracts the ticket from a bounded body, never a URL, and
        supplies the independently minted bootstrap cookie through the headers.
        Return only an opaque value for APP_COOKIE_NAME (Secure, HttpOnly,
        SameSite=Strict, Path=/, no Domain). No native credential is returned.
        """
        if type(method) is not str or method != "POST":
            raise AppRequestError()
        if type(target) is not str or target != CONFIRM_PATH:
            raise AppRequestError()
        nonce = self._cookie(
            method=method,
            headers=headers,
            actual_tls=actual_tls,
            name=_BOOTSTRAP_COOKIE,
        )
        self._require_current()
        try:
            token = (
                self._sign_in.redeem(ticket, browser_nonce=nonce)
                if self._bootstrap_current(nonce) is True
                else None
            )
        except Exception:  # noqa: BLE001 - keep bootstrap and pairing diagnostics private.
            token = None
        self._require_current()
        if token is None:
            raise AppRequestError()
        return token

    def dispatch(self, *, method, target, headers, actual_tls, cancel, run):
        """Call trusted work synchronously with a private cookie and registered use.

        ``cancel`` must already be installed on this unique transport, be
        thread-safe and nonblocking, and latch cancellation before startup.
        ``run(endpoint, method, target, upstream_headers, use)`` gets only
        reconstructed Host, Origin and private native Cookie. It must honor
        cancellation before and during startup and finish cleanup before it
        returns. It may return only a browser-safe permit, never credentials,
        raw upstream responses, or deferred work. Those transport obligations
        cannot be enforced by sampling ``use.cancelled`` alone.
        """
        if type(method) is not str or method != "GET":
            raise AppRequestError()
        if type(target) is not str or target not in _METADATA_PATHS:
            raise AppRequestError()
        if not callable(cancel) or not callable(run):
            raise AppRequestError()
        token = self._cookie(
            method=method, headers=headers, actual_tls=actual_tls, name=APP_COOKIE_NAME
        )
        session = self._session(token)
        with self._admission.connection(self._binding, session, cancel=cancel) as use:
            if use is None:
                if self._session(token) is not session:
                    raise StaleAppSessionError()
                raise AppRequestError()  # Connection capacity, not stale authority.
            self._require_current()
            if (
                use.cancelled
                or type(session.native_cookie) is not web_native_pair.NativeCookie
            ):
                raise StaleAppSessionError()
            try:
                native_cookie = session.native_cookie.upstream_cookie(
                    binding=self._binding, endpoint=self._endpoint, origin=self._origin
                )
            except web_native_pair.NativePairError:
                native_cookie = None
            if native_cookie is None:
                # A private credential that no longer matches this lease
                # is not a reason to pair again or try another endpoint.
                self.close()
                raise StaleAppStateError()
            if use.cancelled:
                self._require_current()
                raise StaleAppSessionError()
            failed = False
            try:
                permit = run(
                    self._endpoint,
                    method,
                    target,
                    (
                        ("Host", self._origin.value.removeprefix("https://")),
                        ("Origin", self._origin.value),
                        ("Cookie", native_cookie),
                    ),
                    use,
                )
            except Exception:  # noqa: BLE001 - keep transport diagnostics private.
                failed = True
            self._require_current()
            if use.cancelled:
                raise StaleAppSessionError()
            if failed:
                raise AppRequestError()
            return permit

    def logout(self, *, method, target, headers, actual_tls, timeout=5.0) -> bool:
        """Revoke this browser's session then wait for its registered uses.

        Call outside the revoked workers. False means revocation took effect
        but cancellation/drain was not confirmed within the deadline; it
        does not make the cookie reusable or revoke native credentials.
        """
        if type(method) is not str or method != "POST":
            raise AppRequestError()
        if type(target) is not str or target != LOGOUT_PATH:
            raise AppRequestError()
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise AppRequestError()
        token = self._cookie(
            method=method, headers=headers, actual_tls=actual_tls, name=APP_COOKIE_NAME
        )
        revocation = self._admission.begin_revocation(
            self._binding, self._session(token)
        )
        if not revocation.revoked:
            self._require_current()
            raise StaleAppSessionError()
        return revocation.wait_drained(timeout)

    def expire(self):
        """Sweep idle state and check for owner loss from the host lifecycle timer."""
        self._require_current()
        self._sign_in.expire()
        self._require_current()

    def close(self):
        """Fence and cancel the lifetime; drain all workers before owner release."""
        self._sign_in.close()
