"""Unserved, authenticated native metadata exchange for named HTTPS.

The host must already hold and verify the private owner lease, and provide a
connector for that owner's private namespace. There is deliberately no default
connector (in particular, no host-loopback dial using a container's port).
"""

import json
import re
import socket
from threading import Lock

if __package__:
    from . import web_app_ingress as app
    from . import web_named_transport as https
    from . import web_native_pair as pair
else:
    import web_app_ingress as app
    import web_named_transport as https
    import web_native_pair as pair


_HEAD_LIMIT = 8192
_LENGTH = re.compile(r"[1-9][0-9]{0,3}")
_SESSION_ID = re.compile(r"ses[A-Za-z0-9_-]{1,128}")
_UNSUPPORTED = frozenset(
    (
        "transfer-encoding",
        "trailer",
        "upgrade",
        "location",
        "refresh",
        "www-authenticate",
        "proxy-authenticate",
        "authentication-info",
        "proxy-authentication-info",
    )
)


def _refuse():
    raise app.AppRequestError()


class _RegisteredIO:
    """Own only this request's sockets, from startup through TLS write."""

    def __init__(self, browser):
        self.browser = browser
        self.upstream = None
        self._lock = Lock()
        self._cancelled = False

    def track(self, upstream):
        if type(upstream) is not socket.socket:
            _refuse()
        with self._lock:
            if upstream is self.upstream:
                _refuse()
            if (
                self._cancelled
                or self.upstream is not None
                or (upstream.type != socket.SOCK_STREAM)
            ):
                upstream.close()
                _refuse()
            self.upstream = upstream
        return upstream

    def cancel(self):
        # Revocation must not wait on I/O or close a borrowed/reused socket.
        # Worker cleanup owns close; shutdown wakes a running read or write.
        with self._lock:
            self._cancelled = True
            for peer in (self.browser, self.upstream):
                if peer is not None:
                    try:
                        # SSLSocket.shutdown clears its TLS object. Leave TLS
                        # state to the worker; interrupt only the underlying TCP.
                        socket.socket.shutdown(peer, socket.SHUT_RDWR)
                    except OSError:
                        pass  # May not yet be connected, or may already be closed.

    def close(self):
        with self._lock:
            try:
                if self.upstream is not None:
                    self.upstream.close()
            finally:
                self.browser.close()


def _recv(peer, count, deadline, check):
    check()
    peer.settimeout(https._remaining(deadline))
    return peer.recv(count)


def _read_reply(peer, *, deadline, check):
    """Require one final, complete, fixed-length response and EOF."""
    pending = bytearray()
    boundary = https._HEAD_END
    scan = 0
    while (end := pending.find(boundary, scan)) < 0:
        if len(pending) == _HEAD_LIMIT:
            _refuse()
        scan = max(0, len(pending) - len(boundary) + 1)
        chunk = _recv(peer, min(4096, _HEAD_LIMIT - len(pending)), deadline, check)
        if not chunk:
            _refuse()
        pending.extend(chunk)
    if pending.count(b"\r\n", 0, end) > https.web_named_headers._MAX_FIELDS:
        _refuse()
    split = end + len(boundary)
    status, headers, _ = pair._reply(bytes(pending[:split]))
    length = headers.get("content-length", ())
    if (
        status != 200
        or headers.keys() & _UNSUPPORTED
        or headers.get("content-type") != ["application/json"]
        or headers.get("content-encoding", []) not in ([], ["identity"])
        or headers.get("connection", []) not in ([], ["close"])
        or len(length) != 1
        or _LENGTH.fullmatch(length[0]) is None
        or (size := int(length[0])) > https._RESPONSE_LIMIT
    ):
        _refuse()
    body = pending[split:]
    if len(body) > size:
        _refuse()
    while len(body) < size:
        chunk = _recv(peer, min(4096, size - len(body)), deadline, check)
        if not chunk:
            _refuse()
        body.extend(chunk)
    if _recv(peer, 1, deadline, check):
        _refuse()
    return bytes(body)


def _metadata(body, target, endpoint):
    # Keep the original bytes only for closed, metadata-only schemas. In
    # particular there are no arbitrary strings, error payloads or extra keys.
    value = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=pair._unique,
        parse_constant=lambda _: _refuse(),
    )
    if type(value) is not dict:
        _refuse()
    if target == "/api/info":
        if (
            value.keys() != {"version", "pid", "urls"}
            or value["version"] != pair._VERSION
            or type(value["pid"]) is not int
            or not 0 < value["pid"] <= 2**31 - 1
            or value["urls"] != [f"http://127.0.0.1:{pair._port(endpoint)}"]
        ):
            _refuse()
    elif target == "/api/session/active":
        if (
            value.keys() != {"data"}
            or type(value["data"]) is not dict
            or any(
                _SESSION_ID.fullmatch(ident) is None or state != {"type": "running"}
                for ident, state in value["data"].items()
            )
        ):
            _refuse()
    else:
        _refuse()


class NamedNativeRelay:
    """Trusted composition with the app's *same* admission and lease.

    ``connect_native(endpoint, *, track, check, deadline)`` must synchronously
    establish one plain stream to that verified endpoint in its own namespace.
    Give the new socket to ``track`` before connecting, call ``check`` before
    and during startup, and observe the supplied monotonic deadline. It must
    return None, send no request bytes and leave no independent work running.
    The relay owns the tracked socket even if startup fails. This contract does
    not preempt a faulty connector or establish a real owner bridge.
    """

    def __init__(self, ingress, *, connect_native):
        if type(ingress) is not app.NamedAppIngress or not callable(connect_native):
            raise ValueError("Invalid named native relay composition.")
        self._ingress = ingress
        self._connect_native = connect_native

    def respond_one(self, reader, peer):
        """Take one accepted workspace TLS connection; never bind or accept."""
        if type(reader) is not https.NamedHTTPSReader:
            with peer:
                _refuse()
        reader._exchange(peer, handlers=None, native=self._respond)

    def _respond(self, stream, request, deadline):
        if (
            type(request) is not https.NamedRequest
            or request.role != "workspace"
            or request.method != "GET"
            or request.target not in app._METADATA_PATHS
            or request.body != b""
        ):
            _refuse()
        ingress = self._ingress
        token = ingress._cookie(
            method=request.method,
            headers=request.headers,
            actual_tls=True,  # Only entered by the TLS reader after parsing.
            name=app.APP_COOKIE_NAME,
            body=request.body,
        )
        # Unlike fixed status, same-origin Fetch Metadata alone is not enough.
        if not any(name.lower() == "origin" for name, _ in request.headers):
            _refuse()
        session = ingress._session(token)
        deadline = min(deadline, session.expires_at)
        io = _RegisteredIO(stream)
        with ingress._admission.connection(
            ingress._binding, session, cancel=io.cancel
        ) as use:
            try:

                def check():
                    if use is None or use.cancelled:
                        _refuse()
                    https._remaining(deadline)

                def current():
                    check()
                    ingress._require_current()
                    check()

                def track(peer):
                    peer = io.track(peer)
                    current()
                    return peer

                current()
                if type(session.native_cookie) is not pair.NativeCookie:
                    _refuse()
                try:
                    cookie = session.native_cookie.upstream_cookie(
                        binding=ingress._binding,
                        endpoint=ingress._endpoint,
                        origin=ingress._origin,
                    )
                except pair.NativePairError:
                    ingress.close()  # Never rebind or repair by pairing again.
                    _refuse()
                current()
                outcome = self._connect_native(
                    ingress._endpoint, track=track, check=current, deadline=deadline
                )
                current()
                if outcome is not None or io.upstream is None:
                    _refuse()
                self._exchange(io, request.target, deadline, cookie, check, current)
            finally:
                io.close()  # Drain includes both closes, not just revocation.

    def _exchange(self, io, target, deadline, cookie, check, current):
        ingress = self._ingress
        origin = ingress._origin.value
        fields = (
            f"GET {target} HTTP/1.1",
            f"Host: {origin.removeprefix('https://')}",
            f"Origin: {origin}",
            f"Cookie: {cookie}",
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: close",
        )
        peer = io.upstream
        wire = ("\r\n".join(fields) + "\r\n\r\n").encode("ascii")
        current()
        peer.settimeout(https._remaining(deadline))
        peer.sendall(wire)
        body = _read_reply(peer, deadline=deadline, check=check)
        _metadata(body, target, ingress._endpoint)
        # Emit host-owned headers only. Never copy native cookies (including
        # rotations), headers, reason phrases or response status to the client.
        https._write_response(
            io.browser, https.NamedResponse(200, body), deadline, check=current
        )
