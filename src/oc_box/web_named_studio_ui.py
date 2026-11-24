"""Pinned named Studio document and assets, never a native proxy."""

import socket
from types import MappingProxyType

if __package__:
    from . import (
        web_app_ingress,
        web_named_account_assets,
        web_named_headers,
        web_named_services,
        web_named_transport,
        web_sessions,
        web_studio_selection,
    )
else:
    import web_app_ingress
    import web_named_account_assets
    import web_named_headers
    import web_named_services
    import web_named_transport
    import web_sessions
    import web_studio_selection


_CSS = "text/css; charset=utf-8"
_JS = "text/javascript; charset=utf-8"
_PINNED = (
    (
        "web_sessions.css",
        18842,
        "a5f53d5601106bccf5a5acca1614c724d727fbda4d160b9c2ce18e0622175c91",
    ),
    (
        "web_sessions.js",
        48491,
        "d0bf72ffea4dfe80737116244a26801381a2bbbe1eabca48c04fd008b36df9db",
    ),
    (
        "web_studio_shell.css",
        1293,
        "06b5dba1306c41805c169edebb06b28a1511db7dc1b49b76d5ddcfe2665638b4",
    ),
    (
        "web_studio_shell.mjs",
        6221,
        "dc78d20318f9235b57242ea3cd19f532a5c828bba94927bb1bb202e13b17d18b",
    ),
    (
        "web_studio_transcript.css",
        3010,
        "6606b20203d72d7fb68e8ba05b7adeb0102e9de194e42da135a50b7eed193f53",
    ),
    (
        "web_studio_transcript.mjs",
        7401,
        "306d1d756cf78ad0ea69aeda404a5d2d4bb58ba13d9aafcf0c4f6d3c3ac56b79",
    ),
    (
        "web_studio_composer.css",
        4052,
        "5177cc4ce743cfe33334ab20e551aecf6ae5864322425086d634bdf921462aa2",
    ),
    (
        "web_studio_composer.mjs",
        8696,
        "dd31d81b1ee7e79ca370b3cd573a300e03c9baa0d05efa1366bd31fee359593b",
    ),
    (
        "web_named_studio_client.mjs",
        10699,
        "463ff82ee59308ae43125a5f398f5edeec1ede60c605ba81d97e81af8fc4bd21",
    ),
    (
        "web_named_studio_entry.mjs",
        4398,
        "ea7e38f6e1bdec7109298ddbd443c078031b2f426e64f7aa92a1b9f19ffb8f7b",
    ),
)
ASSET_PATHS = frozenset(
    (web_sessions.NAMED_STUDIO_ASSET_PREFIX + name).encode("ascii")
    for name, _, _ in _PINNED
)
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_PRIVATE = (
    b"Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
    b"X-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
    b"Cross-Origin-Resource-Policy: same-origin\r\n"
)


def _refuse():
    raise web_named_headers.NamedRequestError() from None


def _interrupt(stream):
    try:
        socket.socket.shutdown(stream, socket.SHUT_RDWR)
    except OSError:
        pass


class NamedStudioUI:
    """First-party literal assets for the same borrowed named TLS worker."""

    def __init__(self, services, registry):
        if (
            type(services) is not web_named_services.NamedServices
            or type(registry) is not web_studio_selection.SelectionRegistry
            or registry._owner is not services._owner._lease.identity
            or registry._admission is not services.application.ingress._admission
        ):
            _refuse()
        self._app = services.application.ingress
        self._registry = registry
        leaf = services._owner._lease.project.rsplit("/", 1)[-1]
        self._name = (
            "".join(char for char in leaf[:80] if char.isprintable()).strip()
            or "Private workspace"
        )
        self._assets = MappingProxyType(
            {
                web_sessions.NAMED_STUDIO_ASSET_PREFIX
                + name: web_named_account_assets._source(name, size, digest)
                for name, size, digest in _PINNED
            }
        )

    def respond(self, stream, request, deadline, *, check):
        try:
            self._respond(stream, request, deadline, check=check)
        except (ValueError, OSError):
            _refuse()

    def _respond(self, stream, request, deadline, *, check):
        app = self._app
        if (
            type(request) is not web_named_transport.NamedRequest
            or request.role != "workspace"
            or request.method != "GET"
            or request.body != b""
            or not callable(check)
        ):
            _refuse()
        document = request.target == "/"
        if not document and request.target not in self._assets:
            _refuse()
        # A browser's direct top-level navigation has Fetch-Site "none".
        # Admit that only for this exact document; the generic app fetch
        # ingress deliberately excludes it for metadata and actions.
        token = web_named_headers.admit_headers(
            origin=app._origin,
            method=request.method,
            headers=request.headers,
            actual_tls=True,
            cookie_name=web_app_ingress.APP_COOKIE_NAME,
        )
        fields = web_named_headers._fields(
            request.headers, app._origin.value.removeprefix("https://")
        )
        css = request.target.endswith(".css")
        mode = (
            "navigate"
            if document
            else "cors"
            if request.target.endswith(".mjs")
            else "no-cors"
        )
        dest = "document" if document else "style" if css else "script"
        allowed_sites = ("none", "same-origin") if document else ("same-origin",)
        if (
            fields.get("sec-fetch-site") not in allowed_sites
            or fields.get("sec-fetch-mode") != mode
            or fields.get("sec-fetch-dest") != dest
            or fields.keys() & web_named_account_assets._FRAMING
            or fields.get("content-length", "0") != "0"
            or fields.get("connection", "close").lower() not in ("close", "keep-alive")
            or any(name.startswith("sec-websocket-") for name in fields)
        ):
            _refuse()
        check()
        browser = app._session(token)
        deadline = min(deadline, browser.expires_at)
        page = None
        delivered = False
        try:
            with app._admission.connection(
                app._binding, browser, cancel=lambda: _interrupt(stream)
            ) as use:

                def current():
                    if use is None or use.cancelled:
                        _refuse()
                    check()
                    app._require_current()
                    if page is not None:
                        self._registry.observe(page, deadline)
                    if use.cancelled:
                        _refuse()

                current()
                if document:
                    page = self._registry.bootstrap(browser, deadline=deadline)
                    body = web_sessions.document(
                        app._origin.value,
                        (),
                        "unavailable",
                        workspace_name=self._name,
                        named_studio=True,
                        named_page=page.public_state(),
                    )
                    mime, policy = "text/html; charset=utf-8", _CSP
                else:
                    body = self._assets[request.target]
                    mime = _CSS if css else _JS
                    policy = "default-src 'none'; frame-ancestors 'none'"
                if not 0 < len(body) <= web_named_transport._DOCUMENT_LIMIT:
                    _refuse()
                wire = (
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                    + _PRIVATE
                    + f"Content-Type: {mime}\r\nContent-Security-Policy: {policy}\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body
                )
                web_named_transport._write_wire(stream, wire, deadline, check=current)
                delivered = True
        finally:
            if page is not None and not delivered:
                # The document might have reached part of the browser. Do
                # not leave its original grant live after uncertain delivery.
                app._admission.begin_revocation(app._binding, browser)
