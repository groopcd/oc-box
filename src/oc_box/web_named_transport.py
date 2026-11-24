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
    from . import web_local_origins, web_named_headers
else:
    import web_local_origins
    import web_named_headers

_REQUEST_TIMEOUT = 5.0
_HEAD_LIMIT = 16384  # Includes the request line and the terminating empty line.
_BODY_LIMIT = 8192
_CONFIRM_SIZE = 54
_RESPONSE_LIMIT = 8192
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


def _head(lines, role, origin):
    if len(lines) > web_named_headers._MAX_FIELDS + 1:
        _refuse()
    start = lines[0].split(b" ", 2)
    if (
        len(start) != 3
        or start[2] != b"HTTP/1.1"
        or tuple(start[:2]) not in _ROUTES[role]
    ):
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
    web_named_headers.admit_headers(
        origin=origin, headers=headers, method=method, actual_tls=True
    )
    fields = {name.lower(): value.strip(" ") for name, value in headers}
    if (
        fields.keys() & _UNSUPPORTED_FIELDS
        or any(name.startswith("sec-websocket-") for name in fields)
        or fields.get("connection", "close").lower() not in ("close", "keep-alive")
        or fields.get("sec-fetch-site") not in (None, "same-origin")
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
    if role == "account":
        if not size or fields.get("content-type") != "application/json":
            _refuse()
    elif target == "/oc-box/confirm":
        if size != _CONFIRM_SIZE:
            _refuse()
    elif size:
        _refuse()  # Workspace metadata and logout are empty.
    return method, target, headers, size


def _read(stream, *, role, origin, deadline):
    wire = bytearray()
    scan = 0
    while (end := wire.find(_HEAD_END, scan)) < 0:
        if len(wire) == _HEAD_LIMIT:
            _refuse()
        scan = max(0, len(wire) - len(_HEAD_END) + 1)
        wire.extend(_recv(stream, min(4096, _HEAD_LIMIT - len(wire)), deadline))
    lines = bytes(wire[:end]).split(b"\r\n", web_named_headers._MAX_FIELDS + 1)
    method, target, headers, size = _head(lines, role, origin)
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


def _write_response(stream, response, deadline):
    if (
        type(response) is not NamedResponse
        or type(response.status) is not int
        or response.status not in _RESPONSE_STATUS
        or type(response.body) is not bytes
        or len(response.body) > _RESPONSE_LIMIT
    ):
        _refuse()
    wire = (
        b"HTTP/1.1 "
        + _RESPONSE_STATUS[response.status]
        + b"\r\n"
        + _RESPONSE_HEADERS
        + b"Content-Length: "
        + str(len(response.body)).encode("ascii")
        + _HEAD_END
        + response.body
    )
    with memoryview(wire) as data:
        offset = 0
        while offset < len(data):
            stream.settimeout(_remaining(deadline))
            count = stream.send(data[offset:])
            if not count:
                _refuse()
            offset += count
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

    def _exchange(self, peer, *, handlers):
        deadline = monotonic() + _REQUEST_TIMEOUT
        try:
            with peer:
                if handlers is not None and not all(map(callable, handlers.values())):
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
                    request = _read(stream, role=role, origin=origin, deadline=deadline)
                    if handlers is None:
                        return request
                    _remaining(deadline)
                    try:
                        response = handlers[role](request, deadline=deadline)
                    except Exception:  # noqa: BLE001 - never echo handler diagnostics.
                        pass
                    else:
                        _write_response(stream, response, deadline)
                        return
        except OSError:
            pass
        _refuse()
