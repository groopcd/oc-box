"""First-party routes on the original private managed loopback Gateway."""

from contextvars import ContextVar
from dataclasses import dataclass
import json
import socket
from time import monotonic

if __package__:
    from . import (
        web_named_account_assets,
        web_named_studio_ui,
        web_sessions,
        web_studio_conversation,
        web_studio_guest as guest,
        web_studio_guest_binding,
        web_studio_guest_cookie_duplex,
        web_studio_guest_ingress as ingress,
        web_studio_guest_read,
        web_studio_guest_selection,
        web_studio_guest_send,
        web_studio_send,
    )
else:
    import web_named_account_assets
    import web_named_studio_ui
    import web_sessions
    import web_studio_conversation
    import web_studio_guest as guest
    import web_studio_guest_binding
    import web_studio_guest_cookie_duplex
    import web_studio_guest_ingress as ingress
    import web_studio_guest_read
    import web_studio_guest_selection
    import web_studio_guest_send
    import web_studio_send


_REASONS = {200: b"OK", 403: b"Forbidden", 503: b"Service Unavailable"}
_JSON = b"application/json"
_CHAT = b"application/json; charset=utf-8"
_HTML = b"text/html; charset=utf-8"
_HEADERS = (
    b"Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
    b"Cross-Origin-Resource-Policy: same-origin\r\nX-Content-Type-Options: nosniff\r\n"
    b"X-Frame-Options: DENY\r\nConnection: close\r\nContent-Security-Policy: "
    + web_named_studio_ui._CSP.encode("ascii")
    + b"\r\n"
)
_NAMESPACES = (b"/oc-box/studio", b"/auth/oc-box-studio-guest")
_PRIVATE_FIELDS = {
    name.encode("ascii") for name in (ingress.APP, ingress.PAGE, ingress.GENERATION)
}
_SEND_STATUS = {
    web_studio_send.SendResult.QUEUED: (200, b'{"status":"queued"}'),
    web_studio_send.SendResult.REFUSED: (403, b'{"status":"refused"}'),
    web_studio_send.SendResult.UNCONFIRMED: (503, b'{"status":"unconfirmed"}'),
}


def _json(value, limit):
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not 0 < len(body) <= limit:
        raise ingress.GuestRequestError()
    return body


def _claim(head):
    # This is only a refusal/routing check. The exact parser still rejects
    # unusual whitespace and non-origin-form requests; none of those forms
    # may send guest headers to the native fallback when claiming our path.
    line, *fields = head.split(b"\r\n")
    if any(
        field.partition(b":")[0].strip().lower() in _PRIVATE_FIELDS for field in fields
    ):
        return True
    segments = line.split(None, 2)
    if len(segments) < 2:
        return False
    target = segments[1].split(b"?", 1)[0]
    if target[:8].lower().startswith((b"http://", b"https://")):
        _, _, authority = target.partition(b"://")
        _, boundary, path = authority.partition(b"/")
        if not boundary:
            return False
        target = b"/" + path
    path = target.split(b"#", 1)[0]
    return any(path == name or path.startswith(name + b"/") for name in _NAMESPACES)


@dataclass(slots=True, repr=False)
class _Exchange:
    admission: guest.GuestAdmission
    verifier: web_studio_guest_cookie_duplex.DuplexGuestCookieVerifier
    registration_returned: bool = False
    use: guest.GuestUse | None = None
    relay_confirmed: bool = True

    def finish(self, closed):
        if not self.registration_returned:
            return False
        confirmed = closed and self.relay_confirmed and self.verifier.cleanup_confirmed
        return (
            confirmed
            if self.use is None
            else self.admission.finish(self.use, confirmed=confirmed)
        )


class _GuestStudioHttp:
    """Own only guest HTTP on one already-accounted listening reservation."""

    def __init__(self, scope, workers):
        if (
            type(scope) is not web_studio_guest_binding._ManagedBinding
            or not scope.accepts_registry(workers)
            or scope.current() is not scope.native.binding
        ):
            raise ingress.GuestRequestError()
        self._scope = scope
        self._binding = scope.native.binding
        self._workers = workers
        self._verifier = ContextVar("oc-box-studio-guest-verifier", default=None)
        self._assets = {
            web_sessions.NAMED_STUDIO_ASSET_PREFIX + name: (
                "style" if name.endswith(".css") else "script",
                b"text/css; charset=utf-8"
                if name.endswith(".css")
                else b"text/javascript; charset=utf-8",
                web_named_account_assets._source(name, size, digest),
            )
            for name, size, digest in web_named_studio_ui._PINNED
        }
        self._admission = guest.GuestAdmission(
            self._binding,
            observe_gateway=self._observe,
            verify_native=self._verify_cookie,
        )
        self._views = web_studio_guest_selection.GuestSelections(
            self._admission, self._binding
        )

    def _observe(self, binding, *, use, deadline):
        if binding is self._binding and not use.cancelled and monotonic() < deadline:
            result = self._scope.current()
            if not use.cancelled and monotonic() < deadline:
                return result
        return None

    def _verify_cookie(self, binding, **kw):
        attempt = self._verifier.get()
        return attempt(binding, **kw) if attempt is not None else None

    def expire(self):
        self._admission.expire()

    def close(self):
        self._admission.close()
        self._views.close()

    def drained(self):
        return self._admission.wait_drained(0)

    def _alive(self, worker, deadline):
        return self._workers.current(worker) and monotonic() < deadline

    def _live(self, worker, deadline):
        return (
            self._alive(worker, deadline)
            and self._scope.current() is self._binding
            and self._alive(worker, deadline)
        )

    def _remaining(self, worker, deadline):
        if not self._alive(worker, deadline) or (left := deadline - monotonic()) <= 0:
            raise ingress.GuestRequestError()
        return left

    def _read_body(self, worker, deadline, data, length):
        if len(data) > length:
            raise ingress.GuestRequestError()
        body = bytearray(data)
        stream = worker.stream
        while len(body) < length:
            stream.settimeout(self._remaining(worker, deadline))
            part = stream.recv(min(4096, length - len(body)))
            if not part:
                raise ingress.GuestRequestError()
            body.extend(part)
        self._remaining(worker, deadline)
        stream.setblocking(False)
        try:
            # The claimed path only handles one exchange. A queued surplus
            # request or observed peer closure never admits native work.
            stream.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return bytes(body)
        raise ingress.GuestRequestError()

    def _reply(self, worker, deadline, status, mime, body, *, current=None):
        header = (
            b"HTTP/1.1 "
            + str(status).encode("ascii")
            + b" "
            + _REASONS[status]
            + b"\r\n"
            b"Content-Type: "
            + mime
            + b"\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n"
            + _HEADERS
            + b"\r\n"
        )
        for value in (header, body):
            view = memoryview(value)
            while view:
                if current is None:
                    if not self._live(worker, deadline):
                        raise ingress.GuestRequestError()
                elif not current():
                    raise ingress.GuestRequestError()
                worker.stream.settimeout(self._remaining(worker, deadline))
                count = worker.stream.send(view[:65536])
                if count <= 0:
                    raise ingress.GuestRequestError()
                view = view[count:]

    def _static(self, worker, deadline, head, data):
        method, target, fields = ingress._head(self._binding, head)
        asset = self._assets.get(target)
        if target != guest.PAGE_PATH and asset is None:
            return False
        if (
            method != "GET"
            or fields.get("content-length", "0") != "0"
            or fields.keys()
            & {"content-type", ingress.APP, ingress.PAGE, ingress.GENERATION}
        ):
            raise ingress.GuestRequestError()
        site, mode, dest = (
            fields.get("sec-fetch-" + name) for name in ("site", "mode", "dest")
        )
        if asset is None:
            if (
                site not in ("same-origin", "none")
                or mode != "navigate"
                or dest != "document"
                or fields.get("sec-fetch-user") not in (None, "?1")
            ):
                raise ingress.GuestRequestError()
            self._read_body(worker, deadline, data, 0)
            challenge = self._admission.offer(self._binding)
            if challenge is None:
                raise ingress.GuestRequestError()
            self._reply(
                worker,
                deadline,
                200,
                _HTML,
                web_sessions.guest_studio_document(challenge=challenge.value),
            )
        else:
            kind, mime, body = asset
            if (
                site != "same-origin"
                or mode not in ("cors", "same-origin", "no-cors")
                or dest != kind
                or "sec-fetch-user" in fields
            ):
                raise ingress.GuestRequestError()
            self._read_body(worker, deadline, data, 0)
            self._reply(worker, deadline, 200, mime, body)
        return True

    def _dispatch(self, worker, deadline, message, exchange):
        admission = self._admission

        def cancel():
            exchange.verifier.cancel()
            self._workers.interrupt(worker)

        if type(message) is guest.ConfirmationRequest:
            result = admission.confirm(
                self._binding, message, cancel=cancel, deadline=deadline
            )
            exchange.registration_returned = True
            if result is None:
                return False
            exchange.use, bootstrap = result
            if bootstrap is None:
                return False
            delivered = False
            try:
                self._reply(
                    worker,
                    deadline,
                    200,
                    _JSON,
                    _json({"app": bootstrap.app, "page": bootstrap.page}, 128),
                    current=lambda: admission.current(exchange.use),
                )
                delivered = admission.current(exchange.use)
            finally:
                if not delivered:
                    admission.revoke(self._binding, bootstrap.grant)
            return True  # Never write a second response.

        result = admission.start(
            self._binding,
            app=message.app,
            page=message.page,
            cancel=cancel,
            deadline=deadline,
        )
        exchange.registration_returned = True
        if result is None:
            return False
        use, grant = result
        exchange.use = use
        if grant is None:
            return False
        operation = message.operation
        args = (self._scope, self._workers, worker, admission, use, grant, self._views)
        scoped = type(operation) in (web_studio_guest_read.Transcript, ingress.Send)
        if type(operation) is ingress.Send:
            outcome, exchange.relay_confirmed = web_studio_guest_send.send(
                *args,
                session_id=operation.session_id,
                generation=operation.generation,
                text=operation.text,
                cookie=message.cookie,
            )
            status, body = _SEND_STATUS[outcome]
            mime = _CHAT
        else:
            value, exchange.relay_confirmed = web_studio_guest_read.read(
                *args, operation=operation, cookie=message.cookie
            )
            if value is None:
                return False
            mime, limit = (
                (_CHAT, web_studio_conversation.MAX_OUTPUT_BYTES)
                if scoped
                else (_JSON, 8192)
            )
            status, body = 200, _json(value, limit)

        if not exchange.relay_confirmed:
            return True

        def current():
            if scoped and status == 200:
                return (
                    self._views.snapshot(
                        use,
                        session_id=operation.session_id,
                        generation=operation.generation,
                    )
                    is not None
                )
            if type(operation) is web_studio_guest_read.Select and status == 200:
                return self._views.state(use).public_state() == value
            return admission.native_current(use)

        self._reply(worker, deadline, status, mime, body, current=current)
        return True

    def handle(self, worker, initial):
        """Return False only for a native, unclaimed request; never retry a route."""
        head, found, extra = initial.partition(b"\r\n\r\n")
        if not found or not _claim(head):
            return False
        deadline = monotonic() + 8
        try:
            if not self._live(worker, deadline):
                return True
            if self._static(worker, deadline, head, extra):
                return True
            prepared = ingress.prepare(self._binding, head)
            message = prepared.complete(
                self._read_body(worker, deadline, extra, prepared.length)
            )
            verifier = web_studio_guest_cookie_duplex.DuplexGuestCookieVerifier(
                self._scope, self._workers, worker
            )
            exchange = _Exchange(self._admission, verifier)
            if not self._workers.on_retirement(worker, exchange.finish):
                return True
            token = self._verifier.set(verifier)
            try:
                if self._dispatch(worker, deadline, message, exchange):
                    return True
            finally:
                self._verifier.reset(token)
            self._reply(worker, deadline, 403, _JSON, b'{"status":"refused"}')
        except (OSError, ValueError):
            # A partial response, expired view or uncertain write is never
            # sent again or routed through native fallback.
            pass
        return True
