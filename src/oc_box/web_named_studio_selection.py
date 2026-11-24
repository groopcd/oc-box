"""Narrow first-party selection on the named app's actual browser admission."""

import json

if __package__:
    from . import (
        web_app_ingress,
        web_named_headers,
        web_named_relay,
        web_named_services,
        web_named_transport,
        web_native_pair,
        web_studio_conversation,
        web_studio_selection,
        web_studio_send,
    )
else:
    import web_app_ingress
    import web_named_headers
    import web_named_relay
    import web_named_services
    import web_named_transport
    import web_native_pair
    import web_studio_conversation
    import web_studio_selection
    import web_studio_send


PATH = "/oc-box/studio/selection"
BODY_LIMIT = 512
_MOVED = "?limit=1&order=desc&type=location-switched"


def _refuse():
    raise web_named_headers.NamedRequestError() from None


def _inputs(body):
    if type(body) is not bytes or not 0 < len(body) <= BODY_LIMIT or not body.isascii():
        _refuse()

    def fields(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            _refuse()
        return result

    try:
        value = json.loads(
            body, object_pairs_hook=fields, parse_constant=lambda _: _refuse()
        )
    except (ValueError, RecursionError):
        _refuse()
    if (
        type(value) is not dict
        or value.keys() != {"page", "sequence", "sessionID"}
        or not web_studio_selection._nonce(value["page"])
        or type(value["sequence"]) is not int
        or not 1 <= value["sequence"] <= web_studio_selection._MAX_GENERATION
        or value["sessionID"] is not None
        and not web_studio_selection._session(value["sessionID"])
    ):
        _refuse()
    return value["page"], value["sequence"], value["sessionID"]


def _native_selection(
    io, lease, browser, binding, origin, ident, *, current, check, deadline
):
    # Keep NativeCookie and the verified private namespace server-side. Never
    # forward browser request headers, paths, or any native response bytes.
    owner = lease.identity
    if type(browser.native_cookie) is not web_native_pair.NativeCookie:
        _refuse()
    cookie = browser.native_cookie.upstream_cookie(
        binding=binding,
        endpoint=owner.endpoint,
        origin=origin,
        version=owner.version,
    )
    current()
    outcome = lease.connect_native(
        owner,
        track=web_named_relay._ConnectorRegistration(io, owner.endpoint, current),
        check=current,
        deadline=deadline,
    )
    current()
    peer = io.upstream
    if outcome is not None or peer is None:
        _refuse()
    authority = origin.value.removeprefix("https://")
    route = "/api/session/" + ident
    message_route = route + "/message"
    moved = message_route + _MOVED
    allowed = frozenset(("/api/info", "/api/location", route, moved))

    def exchange(path, *, closing=False):
        if path not in allowed or (closing and path != moved):
            _refuse()
        current()
        wire = (
            f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nOrigin: {origin.value}\r\n"
            f"Cookie: {cookie}\r\nAccept: application/json\r\nAccept-Encoding: identity\r\n"
            f"Connection: {'close' if closing else 'keep-alive'}\r\n\r\n"
        ).encode("ascii")
        peer.settimeout(web_named_transport._remaining(deadline))
        peer.sendall(wire)

        def recv(size):
            check()
            peer.settimeout(web_named_transport._remaining(deadline))
            return peer.recv(size)

        return web_studio_send._read_reply(recv, closing=closing)

    conversation = web_studio_conversation
    before = conversation._snapshot(
        exchange, owner.endpoint, lease.project, route, ident
    )
    conversation._require_unmoved(exchange, message_route)
    after = conversation._snapshot(
        exchange, owner.endpoint, lease.project, route, ident
    )
    conversation._require_unmoved(
        lambda path: exchange(path, closing=True), message_route
    )
    if before != after:
        _refuse()
    io.finish_workspace(deadline, current)
    current()
    return ident


class NamedStudioSelection:
    """Operate only in the original prepared owner's named app, never a guest.

    The route owner supplies the exact TLS worker's parsed request and check.
    It owns the browser socket and closes it after this single exchange.
    """

    def __init__(self, services):
        if type(services) is not web_named_services.NamedServices:
            _refuse()
        self._owner = services._owner
        self._lease = self._owner._lease
        self._app = services.application.ingress
        self.registry = web_studio_selection.SelectionRegistry(
            self._lease.identity,
            self._app._admission,
            self._app._binding,
            observe_owner=self._observe_owner,
        )

    def _observe_owner(self, deadline):
        self._current(deadline)
        return self._lease.identity

    def _current(self, deadline):
        web_named_transport._remaining(deadline)
        self._app._require_current()
        web_named_transport._remaining(deadline)

    def respond(self, stream, request, deadline, *, check):
        """Verify and publish one page selection. No fallback or automatic retry."""
        try:
            self._respond(stream, request, deadline, check=check)
        except (
            OSError,
            ValueError,
            OverflowError,
            web_native_pair.NativePairError,
        ):
            _refuse()

    def _respond(self, stream, request, deadline, *, check):
        app = self._app
        if (
            type(request) is not web_named_transport.NamedRequest
            or request.role != "workspace"
            or request.method != "POST"
            or request.target != PATH
            or not callable(check)
        ):
            _refuse()
        check()
        token = app._cookie(
            method=request.method,
            headers=request.headers,
            actual_tls=True,
            name=web_app_ingress.APP_COOKIE_NAME,
            body=request.body,
        )
        fields = web_named_headers._fields(
            request.headers, app._origin.value.removeprefix("https://")
        )
        if (
            fields.get("sec-fetch-site") != "same-origin"
            or fields.get("sec-fetch-mode") != "same-origin"
            or fields.get("sec-fetch-dest") != "empty"
            or fields.get("content-type") != "application/json"
        ):
            _refuse()
        page, sequence, ident = _inputs(request.body)
        browser = app._session(token)
        deadline = min(deadline, browser.expires_at)
        io = web_named_relay._RegisteredIO(stream, owns_browser=False)
        with app._admission.connection(app._binding, browser, cancel=io.cancel) as use:
            try:

                def registered():
                    if use is None or use.cancelled:
                        _refuse()
                    check()
                    web_named_transport._remaining(deadline)
                    if use.cancelled:
                        _refuse()

                def current():
                    registered()
                    self._current(deadline)
                    registered()

                def verify(owner, candidate_browser, selected, end):
                    if (
                        owner != self._lease.identity
                        or candidate_browser is not browser
                    ):
                        _refuse()
                    return _native_selection(
                        io,
                        self._lease,
                        browser,
                        app._binding,
                        app._origin,
                        selected,
                        current=current,
                        check=registered,
                        deadline=end,
                    )

                current()
                view = self.registry.change(
                    browser,
                    page,
                    sequence=sequence,
                    session_id=ident,
                    deadline=deadline,
                    verify=verify,
                )
                if not io.cleanup_confirmed:
                    _refuse()

                def delivery():
                    registered()
                    self.registry.observe(view, deadline)
                    registered()

                body = json.dumps(view.public_state(), separators=(",", ":")).encode(
                    "ascii"
                )
                web_named_transport._write_response(
                    stream,
                    web_named_transport.NamedResponse(200, body),
                    deadline,
                    check=delivery,
                )
            finally:
                try:
                    io.close()
                finally:
                    if use is not None and not io.cleanup_confirmed:
                        use._cleanup_unconfirmed.set()
                        self.registry.close()
                        app.close()
                        _refuse()
