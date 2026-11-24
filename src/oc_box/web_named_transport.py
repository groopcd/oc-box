"""Unserved, one-request transport for the local named HTTPS profile.

This only uses an already accepted socket. It does not bind or accept, choose
a workspace or upstream, or provide a production dispatcher.
"""

import re
import socket
import ssl
from dataclasses import dataclass
from time import monotonic

if __package__:
    from . import (
        web_local_origins,
        web_named_bootstrap,
        web_named_handoff,
        web_named_headers,
        web_named_listeners,
    )
else:
    import web_local_origins
    import web_named_bootstrap
    import web_named_handoff
    import web_named_headers
    import web_named_listeners

_REQUEST_TIMEOUT = 5.0
_HEAD_LIMIT = 16384  # Includes the request line and the terminating empty line.
_BODY_LIMIT = 8192
_CONFIRM_SIZE = 54
_ENTRY_FORM_SIZE = 50
_HANDOFF_FORM_SIZE = 61
_RESPONSE_LIMIT = 8192
_DOCUMENT_LIMIT = 48 * 1024
_HEAD_END = b"\r\n\r\n"
_LENGTH = re.compile(r"0|[1-9][0-9]{0,3}")
_UNSUPPORTED_FIELDS = frozenset(
    (
        "transfer-encoding",
        "content-encoding",
        "trailer",
        "te",
        "expect",
        "proxy-connection",
        "http2-settings",
        "upgrade",
    )
)
# A transport ceiling, not authorization. Exact targets only: no aliases,
# queries, normalization, native pairing, upgrades, or cross-site receiver.
_ROUTES = {
    "account": frozenset(((b"POST", b"/start"), (b"POST", b"/finish"))),
    "workspace": frozenset(
        (
            (b"POST", b"/oc-box/confirm"),
            (b"POST", b"/oc-box/logout"),
            (b"GET", b"/api/info"),
            (b"GET", b"/api/session/active"),
        )
    ),
}
_ENTRY_ROUTES = {
    "account": (b"POST", web_named_bootstrap.ACCOUNT_ENTRY.encode("ascii")),
    "workspace": (b"GET", web_named_bootstrap.WORKSPACE_ENTRY.encode("ascii")),
}
_RESPONSE_STATUS = {
    200: b"200 OK",
    403: b"403 Forbidden",
    503: b"503 Service Unavailable",
}
_RESPONSE_HEADERS = (
    b"Connection: close\r\n"
    b"Content-Type: application/json\r\n"
    b"Cache-Control: no-store\r\n"
    b"Referrer-Policy: no-referrer\r\n"
    b"X-Content-Type-Options: nosniff\r\n"
    b"Cross-Origin-Resource-Policy: same-origin\r\n"
    b"Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; "
    b"base-uri 'none'; form-action 'none'\r\n"
)


@dataclass(frozen=True, slots=True, repr=False)
class NamedRequest:
    """Transport-admitted wire data; headers and body must not be logged."""

    role: str
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True, repr=False)
class NamedResponse:
    """Fixed JSON envelope for a trusted research dispatcher, not a proxy."""

    status: int
    body: bytes


def _refuse():
    raise web_named_headers.NamedRequestError()


def _remaining(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        _refuse()
    return remaining


def _recv(stream, size, deadline):
    stream.settimeout(_remaining(deadline))
    data = stream.recv(size)
    if not data:
        _refuse()
    return data


def _head(lines, role, origin, *, entry_origins=None, include_core=False):
    if len(lines) > web_named_headers._MAX_FIELDS + 1:
        _refuse()
    start = lines[0].split(b" ", 2)
    route = tuple(start[:2])
    entry = entry_origins is not None and route == _ENTRY_ROUTES[role]
    core = (entry_origins is None or include_core) and route in _ROUTES[role]
    handoff = (
        entry_origins is not None
        and include_core
        and role == "workspace"
        and route == (b"POST", web_named_handoff.HANDOFF_PATH.encode("ascii"))
    )
    if len(start) != 3 or start[2] != b"HTTP/1.1" or not (entry or core or handoff):
        _refuse()
    method, target = start[0].decode("ascii"), start[1].decode("ascii")
    headers = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon or not name.isascii() or not value.isascii():
            _refuse()
        headers.append((name.decode("ascii"), value.decode("ascii")))

    # Check the whole wire header block (including duplicates) before building
    # a dictionary or waiting for the body. The caller has completed TLS.
    headers = tuple(headers)
    form_origin = None
    if entry and role == "account":
        form_origin = entry_origins.workspace
    elif handoff:
        form_origin = entry_origins.account
    web_named_headers.admit_headers(
        origin=origin,
        headers=headers,
        method=method,
        actual_tls=True,
        form_origin=form_origin,
    )
    fields = {name.lower(): value.strip(" ") for name, value in headers}
    sites = (
        ("cross-site",)
        if form_origin is not None
        else ("none", "same-origin")
        if entry
        else (None, "same-origin")
    )
    if (
        fields.keys() & _UNSUPPORTED_FIELDS
        or any(name.startswith("sec-websocket-") for name in fields)
        or fields.get("connection", "close").lower() not in ("close", "keep-alive")
        or fields.get("sec-fetch-site") not in sites
        or (
            (entry or handoff)
            and (
                fields.get("sec-fetch-mode") != "navigate"
                or fields.get("sec-fetch-dest") != "document"
            )
        )
    ):
        _refuse()

    length = fields.get("content-length")
    if method == "POST":
        if length is None or _LENGTH.fullmatch(length) is None:
            _refuse()
        size = int(length)
        if size > _BODY_LIMIT:
            _refuse()
    elif length in (None, "0"):
        size = 0
    else:
        _refuse()
    if form_origin is not None:
        if (
            size != (_HANDOFF_FORM_SIZE if handoff else _ENTRY_FORM_SIZE)
            or fields.get("content-type") != "application/x-www-form-urlencoded"
        ):
            _refuse()
    elif role == "account":
        if not size or fields.get("content-type") != "application/json":
            _refuse()
    elif target == "/oc-box/confirm":
        if size != _CONFIRM_SIZE:
            _refuse()
    elif size:
        _refuse()  # Workspace metadata and logout are empty.
    return method, target, headers, size


def _read(stream, *, role, origin, deadline, entry_origins=None, include_core=False):
    wire = bytearray()
    scan = 0
    while (end := wire.find(_HEAD_END, scan)) < 0:
        if len(wire) == _HEAD_LIMIT:
            _refuse()
        scan = max(0, len(wire) - len(_HEAD_END) + 1)
        wire.extend(_recv(stream, min(4096, _HEAD_LIMIT - len(wire)), deadline))
    lines = bytes(wire[:end]).split(b"\r\n", web_named_headers._MAX_FIELDS + 1)
    method, target, headers, size = _head(
        lines, role, origin, entry_origins=entry_origins, include_core=include_core
    )
    body = wire[end + len(_HEAD_END) :]
    if len(body) > size:
        _refuse()
    while len(body) < size:
        body.extend(_recv(stream, size - len(body), deadline))
    if target == "/oc-box/confirm" and not body.isascii():
        _refuse()

    # Refuse surplus observed during framing or by this one immediate probe.
    # WantRead is not proof that no further bytes are in flight or pending in a
    # partial TLS record. This socket is never used to read a second request.
    _remaining(deadline)
    stream.setblocking(False)
    try:
        stream.recv(1)
    except ssl.SSLWantReadError:
        pass
    else:
        _refuse()  # Surplus data or TLS EOF before admission.
    _remaining(deadline)
    return NamedRequest(role, method, target, headers, bytes(body))


def _write_response(stream, response, deadline, *, check=None, clear_app=False):
    if (
        type(response) is not NamedResponse
        or type(response.status) is not int
        or response.status not in _RESPONSE_STATUS
        or type(response.body) is not bytes
        or len(response.body) > _RESPONSE_LIMIT
        or type(clear_app) is not bool
        or (clear_app and response.status != 200)
    ):
        _refuse()
    cookie = b""
    if clear_app:
        cookie = b"Set-Cookie: __Host-ocbox-app=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0\r\n"
    wire = (
        b"HTTP/1.1 "
        + _RESPONSE_STATUS[response.status]
        + b"\r\n"
        + _RESPONSE_HEADERS
        + cookie
        + b"Content-Length: "
        + str(len(response.body)).encode("ascii")
        + _HEAD_END
        + response.body
    )
    _write_wire(stream, wire, deadline, check=check)


def _write_app_grant(stream, token, deadline, *, check):
    cookie = web_named_bootstrap._cookie("__Host-ocbox-app", token).encode("ascii")
    # 204 responses cannot carry content or Content-Length. No upstream
    # cookie or token-bearing body is ever copied to the browser.
    _write_wire(
        stream,
        b"HTTP/1.1 204 No Content\r\n"
        + _RESPONSE_HEADERS
        + b"Set-Cookie: "
        + cookie
        + b"\r\n\r\n",
        deadline,
        check=check,
    )


def _write_wire(stream, wire, deadline, *, check=None):
    with memoryview(wire) as data:
        offset = 0
        while offset < len(data):
            if check is not None:
                check()
            stream.settimeout(_remaining(deadline))
            count = stream.send(data[offset:])
            if not count:
                _refuse()
            offset += count
    if check is not None:
        check()
    _remaining(deadline)


class NamedHTTPSReader:
    """Private host-side transport; never hand it a shared or borrowed socket."""

    def __init__(
        self, origins, *, account_cert, account_key, workspace_cert, workspace_key
    ):
        # Use the pinned-file validation and exact per-site SNI policies. Do not
        # accept an arbitrary SSLContext or load a certificate on a request path.
        tls = web_local_origins.check_tls(
            origins,
            account_cert=account_cert,
            account_key=account_key,
            workspace_cert=workspace_cert,
            workspace_key=workspace_key,
        )
        self._configure(origins, tls)

    @classmethod
    def from_reservation(cls, reservation):
        """Reuse the already verified per-site TLS contexts, not certificate paths."""
        if (
            type(reservation) is not web_named_listeners.ReservedProfile
            or type(reservation.origins) is not web_local_origins.LocalOrigins
            or type(reservation.tls) is not web_local_origins.LocalTLS
        ):
            _refuse()
        reader = cls.__new__(cls)
        reader._configure(reservation.origins, reservation.tls)
        return reader

    def _configure(self, origins, tls):
        self._origins = origins
        self._endpoints = {
            origins.account_bind: ("account", origins.account, tls.account),
            origins.workspace_bind: ("workspace", origins.workspace, tls.workspace),
        }
        for context in (tls.account, tls.workspace):
            context.set_alpn_protocols(["http/1.1"])
            context.options |= ssl.OP_NO_TICKET | ssl.OP_NO_RENEGOTIATION
            context.num_tickets = 0

    def read_one(self, peer):
        """Take ownership of one connected socket accepted by the trusted caller.

        Derive role from the actual local IPv4 loopback endpoint, never SNI or
        HTTP input. Close both raw and TLS handles on every path before returning
        a request or a fixed refusal. The one deadline covers all on-wire reads.
        The result is not yet a browser session or permission to start work.
        """
        return self._exchange(peer, handlers=None)

    def respond_one(self, peer, *, account, workspace):
        """Parse, call the trusted handler for the selected role, respond and close.

        ``handler(request, deadline=...)`` receives no stream and must return
        a NamedResponse. It owns any authorization checks and must cooperate
        with the same absolute monotonic deadline. This transport cannot
        interrupt synchronous work, undo side effects, or assert completion
        of external work. It refuses to start a write if the budget has expired.
        No retry, graceful TLS drain, or second request is attempted.
        """
        self._exchange(peer, handlers={"account": account, "workspace": workspace})

    def respond_prelogin(self, peer, *, ingress, login_document):
        """Serve the two exact bootstrap documents using trusted local HTML.

        ``login_document`` is a prevalidated static artifact supplied by the
        launcher, never request input. No other routes are dispatched by this
        method and no native credential or grant is issued.
        """
        self._exchange(
            peer,
            handlers=None,
            prelogin=self._prelogin_handler(ingress, login_document),
        )

    def _prelogin_handler(self, ingress, login_document):
        def prelogin(stream, request, deadline):
            if (
                type(ingress) is not web_named_bootstrap.NamedBootstrapIngress
                or ingress._origins != self._origins
                or type(login_document) is not bytes
                or not 0 < len(login_document) <= _DOCUMENT_LIMIT
            ):
                _refuse()
            arguments = dict(
                method=request.method,
                target=request.target,
                headers=request.headers,
                body=request.body,
                actual_tls=True,
            )
            account_origin = self._origins.account.value
            try:
                if (
                    request.role == "workspace"
                    and request.target == web_named_bootstrap.WORKSPACE_ENTRY
                ):
                    page = ingress.workspace(**arguments)
                    body, cookie = page.html, page.cookie
                    cookie_name = "__Host-ocbox-bootstrap"
                    policy = (
                        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action "
                        + account_origin
                    )
                elif (
                    request.role == "account"
                    and request.target == web_named_bootstrap.ACCOUNT_ENTRY
                ):
                    body, cookie = login_document, ingress.account(**arguments).cookie
                    cookie_name = "__Host-ocbox-account"
                    policy = web_named_handoff.account_policy(self._origins)
                else:
                    _refuse()
            except web_named_headers.NamedRequestError:
                _write_response(
                    stream,
                    NamedResponse(403, b'{"error":"Named entry refused."}'),
                    deadline,
                )
                return
            if (
                not 0 < len(body) <= _DOCUMENT_LIMIT
                or re.fullmatch(
                    re.escape(cookie_name)
                    + r"=[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]; Secure; HttpOnly; SameSite=Strict; Path=/",
                    cookie,
                )
                is None
            ):
                _refuse()
            wire = (
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\nReferrer-Policy: origin\r\n"
                b"X-Content-Type-Options: nosniff\r\nCross-Origin-Resource-Policy: same-origin\r\n"
                + f"Content-Security-Policy: {policy}\r\nSet-Cookie: {cookie}\r\n".encode(
                    "ascii"
                )
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            _write_wire(stream, wire, deadline, check=ingress._live)

        return prelogin

    def _exchange(self, peer, *, handlers, native=None, prelogin=None, named=None):
        deadline = monotonic() + _REQUEST_TIMEOUT
        try:
            with peer:
                modes = (handlers, native, prelogin, named)
                if sum(mode is not None for mode in modes) > 1 or (
                    handlers is not None and not all(map(callable, handlers.values()))
                ):
                    _refuse()
                if peer.family != socket.AF_INET or peer.type != socket.SOCK_STREAM:
                    _refuse()
                endpoint = self._endpoints.get(peer.getsockname())
                if (
                    endpoint is None
                    or peer.getpeername()[0] != web_local_origins.LOOPBACK
                ):
                    _refuse()
                role, origin, context = endpoint
                if native is not None and role != "workspace":
                    _refuse()
                with context.wrap_socket(
                    peer,
                    server_side=True,
                    do_handshake_on_connect=False,
                    suppress_ragged_eofs=False,
                ) as stream:
                    stream.settimeout(_remaining(deadline))
                    stream.do_handshake()
                    # Session resumption can reuse SNI from a prior TLS handshake.
                    if stream.session_reused or stream.selected_alpn_protocol() not in (
                        None,
                        "http/1.1",
                    ):
                        _refuse()
                    request = _read(
                        stream,
                        role=role,
                        origin=origin,
                        deadline=deadline,
                        entry_origins=self._origins
                        if prelogin is not None or named is not None
                        else None,
                        include_core=named is not None,
                    )
                    if all(mode is None for mode in modes):
                        return request
                    _remaining(deadline)
                    try:
                        if native is not None:
                            native(stream, request, deadline)
                            return
                        if prelogin is not None:
                            prelogin(stream, request, deadline)
                            return
                        if named is not None:
                            named(stream, request, deadline)
                            return
                        response = handlers[role](request, deadline=deadline)
                    except Exception:  # noqa: BLE001 - never echo handler diagnostics.
                        pass
                    else:
                        _write_response(stream, response, deadline)
                        return
        except OSError:
            pass
        _refuse()
