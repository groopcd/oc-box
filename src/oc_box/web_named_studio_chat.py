"""Unserved first-party Studio conversation and explicit text Send dispatch."""

from dataclasses import dataclass
import os
import re
from threading import Lock
from time import monotonic

if __package__:
    from . import (
        web_app_ingress,
        web_named_headers,
        web_named_owner,
        web_named_transport,
        web_studio_conversation,
        web_studio_read,
        web_studio_selection,
        web_studio_send,
    )
else:
    import web_app_ingress
    import web_named_headers
    import web_named_owner
    import web_named_transport
    import web_studio_conversation
    import web_studio_read
    import web_studio_selection
    import web_studio_send


READ_PATH = web_studio_read.PATH
SEND_PATH = "/oc-box/studio/send"
PAGE_HEADER = "X-OC-Box-Studio-Page"
GENERATION_HEADER = "X-OC-Box-Studio-Generation"
MAX_RESPONSE_BYTES = web_studio_conversation.MAX_OUTPUT_BYTES
_READ_TARGET = re.compile(re.escape(READ_PATH) + r"\?session=(ses[A-Za-z0-9_-]{1,125})")
_GENERATION = re.compile(r"[1-9][0-9]{0,15}")
_FORBIDDEN = web_named_transport._UNSUPPORTED_FIELDS | {
    "idempotency-key",
    "x-idempotency-key",
    "sec-fetch-user",
}


@dataclass(frozen=True, slots=True, repr=False)
class ChatReply:
    """Host-owned response only; the borrowing TLS worker frames the exchange."""

    status: int
    body: bytes
    headers = web_studio_read.ReadResponse.headers + (
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    )


_SEND_REPLIES = {
    web_studio_send.SendResult.REFUSED: ChatReply(403, b'{"status":"refused"}'),
    web_studio_send.SendResult.QUEUED: ChatReply(200, b'{"status":"queued"}'),
    web_studio_send.SendResult.UNCONFIRMED: ChatReply(503, b'{"status":"unconfirmed"}'),
}


def _refuse():
    raise web_named_headers.NamedRequestError() from None


def _remaining(deadline):
    return web_studio_conversation._remaining(deadline)


def _envelope(request, origin, *, actual_tls):
    if (
        actual_tls is not True
        or type(request) is not web_named_transport.NamedRequest
        or type(request.role) is not str
        or request.role != "workspace"
        or type(request.method) is not str
        or type(request.target) is not str
        or type(request.headers) is not tuple
        or type(request.body) is not bytes
    ):
        _refuse()
    reading = request.method == "GET"
    if reading:
        target = _READ_TARGET.fullmatch(request.target)
        if target is None or request.body:
            _refuse()
    elif (
        request.method != "POST"
        or request.target != SEND_PATH
        or not 0 < len(request.body) <= web_studio_send.MAX_BODY_BYTES
    ):
        _refuse()

    fields = web_named_headers._fields(
        request.headers, origin.value.removeprefix("https://")
    )
    supplied = fields.get("origin")
    if (
        fields.keys() & _FORBIDDEN
        or any(name.startswith("sec-websocket-") for name in fields)
        or fields.get("connection", "close").lower() not in ("close", "keep-alive")
        or fields.get("sec-fetch-site") != "same-origin"
        or fields.get("sec-fetch-mode") not in ("cors", "same-origin")
        or fields.get("sec-fetch-dest") != "empty"
        or supplied not in (origin.value, None)
        or (not reading and supplied is None)
    ):
        _refuse()
    page = fields.get(PAGE_HEADER.lower())
    raw_generation = fields.get(GENERATION_HEADER.lower(), "")
    if (
        not web_studio_selection._nonce(page)
        or _GENERATION.fullmatch(raw_generation) is None
        or (generation := int(raw_generation)) > web_studio_selection._MAX_GENERATION
    ):
        _refuse()
    if reading:
        if fields.get("content-length", "0") != "0" or "content-type" in fields:
            _refuse()
        return page, generation, target[1], None

    if fields.get("content-type") != "application/json" or fields.get(
        "content-length"
    ) != str(len(request.body)):
        _refuse()
    body = web_studio_conversation._json(request.body)
    if (
        type(body) is not dict
        or body.keys() != {"sessionID", "text"}
        or not web_studio_selection._session(body["sessionID"])
    ):
        _refuse()
    # Validate exactly the existing Send text contract; keep only the text,
    # not a browser-supplied native payload, ID, agent, delivery, or options.
    web_studio_send._native_body(body["text"])
    return page, generation, body["sessionID"], body["text"]


class NamedStudioChat:
    """Compose only with the existing named app and its exact original owner."""

    def __init__(self, owner, selections):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(owner) is not web_named_owner.NamedOwnerBinding
            or type(selections) is not web_studio_selection.SelectionRegistry
        ):
            _refuse()
        with owner._lock:
            app = owner._app
            lease = owner._lease
            if owner._closed.is_set() or type(app) is not web_named_owner.BoundNamedApp:
                _refuse()
        ingress = app.ingress
        if (
            type(ingress) is not web_app_ingress.NamedAppIngress
            or ingress._binding is not owner.binding
            or ingress._endpoint != lease.identity.endpoint
            or ingress._native_version != web_studio_conversation.NATIVE_VERSION
            or selections._owner is not lease.identity
            or selections._admission is not ingress._admission
        ):
            _refuse()
        self._box = owner._box
        self._app = ingress
        self._lease = lease
        self._selections = selections

    def _current(self, deadline, check):
        _remaining(deadline)
        check()
        self._app._require_current()  # Original named owner and account fence.
        check()
        _remaining(deadline)

    def respond(
        self, request, *, actual_tls, deadline, check, write, interrupt, retire
    ):
        """One exchange in the checked workspace TLS worker; never own its stream.

        ``check`` is the live site guard. ``write(reply, deadline, *, check)``
        must synchronously check each bounded write. ``interrupt`` wakes only
        this request's downstream I/O; ``retire`` positively confirms no
        downstream work remains without taking ownership of the socket.
        Any exception is close-only; do not emit another response or retry.
        """
        try:
            if not all(
                callable(func) for func in (check, write, interrupt, retire)
            ) or type(deadline) not in (int, float):
                _refuse()
            _remaining(deadline)
            app = self._app
            check()
            nonce, generation, ident, text = _envelope(
                request, app._origin, actual_tls=actual_tls
            )
            token = app._cookie(
                method=request.method,
                headers=request.headers,
                actual_tls=actual_tls,
                name=web_app_ingress.APP_COOKIE_NAME,
                body=request.body,
            )
            self._current(deadline, check)
            browser = app._session(token)
            deadline = min(
                deadline,
                browser.expires_at,
                monotonic() + web_studio_conversation.HOST_TIMEOUT,
            )
            self._current(deadline, check)
            view = self._selections.snapshot(
                browser,
                nonce,
                session_id=ident,
                generation=generation,
                deadline=deadline,
            )

            def observe(end):
                self._current(end, check)
                scope = self._selections.observe(view, end)
                if (
                    type(scope) is not web_studio_read.ReadScope
                    or scope is not view.scope
                    or scope.owner is not self._lease.identity
                    or scope.browser is not browser
                ):
                    _refuse()
                self._current(end, check)
                return scope

            scope = observe(deadline)
            if text is None:

                def deliver(reply, end, *, check):
                    if (
                        type(reply) is not web_studio_read.ReadResponse
                        or type(reply.body) is not bytes
                        or not 0 < len(reply.body) <= MAX_RESPONSE_BYTES
                    ):
                        _refuse()
                    check()
                    write(ChatReply(200, reply.body), end, check=check)

                web_studio_read.respond(
                    self._box,
                    web_studio_read.ReadRequest(
                        request.method, request.target, request.body
                    ),
                    scope,
                    lease=self._lease,
                    admission=app._admission,
                    binding=app._binding,
                    observe=observe,
                    deadline=deadline,
                    write=deliver,
                    interrupt=interrupt,
                    retire=retire,
                )
            else:
                self._send(scope, text, observe, deadline, write, interrupt, retire)
            return
        except Exception:
            # Hide request/auth/adapter diagnostics. The outer worker closes
            # this exchange even if a partial response may have been written.
            pass
        _refuse()

    def _send(self, scope, text, observe, deadline, write, interrupt, retire):
        app, lease = self._app, self._lease
        owner = lease.identity
        selected = web_studio_send.SendScope(
            owner.workspace_id,
            owner.generation,
            owner.container_id,
            owner.endpoint,
            lease.project,
            owner.version,
            scope.session_id,
            scope.selection_generation,
            scope.browser,
        )
        use = None
        live_observer, downstream = observe, interrupt

        def current(end):
            if use is None or use.cancelled or live_observer is None:
                _refuse()
            if live_observer(end) is not scope or use.cancelled:
                _refuse()
            return selected

        operation = web_studio_send.StudioSend(
            selected, control=lease.control, observe=current, deadline=deadline
        )
        lock = Lock()

        def cancel():
            with lock:
                try:
                    operation.cancel()
                finally:
                    if downstream is not None:
                        downstream()

        admission = app._admission
        with admission.connection(app._binding, scope.browser, cancel=cancel) as use:
            confirmed = True
            try:
                current(deadline)
                confirmed = False
                outcome = operation.execute(
                    self._box, native_password=lease.native_password, text=text
                )
                confirmed = operation.cleanup_confirmed is True
                if not confirmed or type(outcome) is not web_studio_send.SendResult:
                    _refuse()

                def delivery_check():
                    current(deadline)

                delivery_check()
                write(_SEND_REPLIES[outcome], deadline, check=delivery_check)
                delivery_check()
            finally:
                retired = False
                try:
                    with lock:
                        # A fenced use may retain this operation, but never
                        # retain or interrupt the borrowed TLS worker later.
                        live_observer = downstream = None
                    if use is not None:
                        retired = retire() is True
                finally:
                    if use is not None and (not retired or not confirmed):
                        use._cleanup_unconfirmed.set()
                        admission.close()
                        _refuse()
