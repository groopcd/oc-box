"""One bounded HTTP request on an already counted one-use gateway socket."""

from dataclasses import dataclass
import re

if __package__:
    from . import (
        web_studio_conversation,
        web_studio_guest,
        web_studio_guest_read,
        web_studio_send,
    )
else:
    import web_studio_conversation
    import web_studio_guest
    import web_studio_guest_read
    import web_studio_send


_PREFIX = "/oc-box/studio"
CATALOG = _PREFIX + "/catalog"
SELECTION = _PREFIX + "/selection"
CONVERSATION = _PREFIX + "/conversation"
SEND = _PREFIX + "/send"
APP = "x-oc-box-studio-app"
PAGE = "x-oc-box-studio-page"
GENERATION = "x-oc-box-studio-generation"
_TRANSCRIPT = re.compile(
    re.escape(CONVERSATION) + r"\?session=(ses[A-Za-z0-9_-]{1,125})"
)
_NUMBER = re.compile(r"[1-9][0-9]{0,15}")
_FORBIDDEN = web_studio_guest._FORBIDDEN_HEADERS | {
    "content-encoding",
    "te",
    "range",
    "idempotency-key",
    "x-idempotency-key",
}


class GuestRequestError(ValueError):
    def __init__(self):
        super().__init__("The Studio guest request was not admitted.")


def _refuse():
    raise GuestRequestError() from None


def _positive(value):
    if type(value) is not str or _NUMBER.fullmatch(value) is None:
        _refuse()
    result = int(value)
    if result > 2**53 - 1:
        _refuse()
    return result


@dataclass(frozen=True, slots=True, repr=False)
class Send:
    session_id: str
    generation: int
    text: str


@dataclass(frozen=True, slots=True, repr=False)
class Exchange:
    app: str
    page: str
    cookie: str | None
    operation: object


@dataclass(frozen=True, slots=True, repr=False)
class _Prepared:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    length: int
    app: str | None
    page: str | None
    cookie: str | None
    generation: int | None
    transcript: str | None

    def complete(self, body):
        """Call before dispatch; surplus input must not become another request."""
        if type(body) is not bytes or len(body) != self.length:
            _refuse()
        if self.target == web_studio_guest.CONFIRM_PATH:
            return web_studio_guest.ConfirmationRequest(
                self.method, self.target, self.headers, body
            )
        if self.target == CATALOG:
            operation = web_studio_guest_read.Catalog()
        elif self.transcript is not None:
            operation = web_studio_guest_read.Transcript(
                self.transcript, self.generation
            )
        else:
            try:
                data = web_studio_conversation._json(body)
                if type(data) is not dict:
                    _refuse()
                ident = data.get("sessionID")
                if self.target == SELECTION:
                    sequence = data.get("sequence")
                    if (
                        data.keys() != {"sequence", "sessionID"}
                        or type(sequence) is not int
                        or not 1 <= sequence <= 2**53 - 1
                        or ident is not None
                        and not web_studio_conversation._session_id(ident)
                    ):
                        _refuse()
                    operation = web_studio_guest_read.Select(sequence, ident)
                else:
                    if data.keys() != {
                        "sessionID",
                        "text",
                    } or not web_studio_conversation._session_id(ident):
                        _refuse()
                    web_studio_send._native_body(data["text"])
                    operation = Send(ident, self.generation, data["text"])
            except ValueError:
                _refuse()
        return Exchange(self.app, self.page, self.cookie, operation)


def _head(binding, head):
    gate = web_studio_guest
    if (
        type(binding) is not gate.GuestBinding
        or type(head) is not bytes
        or not 0 < len(head) <= gate._MAX_HEADER_BYTES
        or not head.isascii()
    ):
        _refuse()
    line, *headers = head.decode("ascii").split("\r\n")
    if not 1 <= len(headers) <= 32 or len(line) > 256:
        _refuse()
    parts = line.split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        _refuse()
    method, target, _ = parts
    fields = {}
    for header in headers:
        name, colon, value = header.partition(":")
        name = name.lower()
        if (
            not colon
            or gate._HEADER.fullmatch(name) is None
            or name in fields
            or name in _FORBIDDEN
            or name.startswith(
                ("proxy-", "x-forwarded-", "sec-websocket-", "access-control-request-")
            )
            or any(not 32 <= ord(char) <= 126 for char in value)
        ):
            _refuse()
        fields[name] = value.strip(" ")
    cookie = fields.get("cookie")
    if (
        fields.get("host") != binding.authority
        or fields.get("origin") not in (binding.origin, None)
        or method == "POST"
        and fields.get("origin") != binding.origin
        or fields.get("connection", "close").lower() not in ("close", "keep-alive")
        or cookie is not None
        and gate._native_headers(binding, cookie) is None
    ):
        _refuse()
    return method, target, fields


def prepare(binding, head):
    """Validate a guest request head (excluding CRLF CRLF) before reading its body.

    The acceptor must already own the exact registered socket. This is not a
    native router or authentication. The caller must handle claimed-route
    refusal locally, use one deadline and close rather than reuse the socket.
    """
    gate = web_studio_guest
    method, target, fields = _head(binding, head)
    if (
        fields.get("sec-fetch-site") != "same-origin"
        or fields.get("sec-fetch-mode") != "same-origin"
        or fields.get("sec-fetch-dest") != "empty"
        or "sec-fetch-user" in fields
    ):
        _refuse()
    query = _TRANSCRIPT.fullmatch(target)
    confirmation = target == gate.CONFIRM_PATH
    if target in (SEND, SELECTION) or confirmation:
        if method != "POST":
            _refuse()
    elif method != "GET" or target != CATALOG and query is None:
        _refuse()

    cookie = fields.get("cookie")
    app, page = fields.get(APP), fields.get(PAGE)
    if confirmation:
        if (
            fields.keys() & {APP, PAGE, GENERATION}
            or cookie is None
            or fields.get("content-type") != "text/plain"
            or fields.get("content-length") != "43"
        ):
            _refuse()
        length = 43
    else:
        if not gate._token(app) or not gate._token(page):
            _refuse()
        if method == "GET":
            if fields.get("content-length", "0") != "0" or "content-type" in fields:
                _refuse()
            length = 0
        else:
            if fields.get("content-type") != "application/json":
                _refuse()
            length = _positive(fields.get("content-length"))
            if length > (
                512 if target == SELECTION else web_studio_send.MAX_BODY_BYTES
            ):
                _refuse()
    generation = None
    if target == SEND or query is not None:
        generation = _positive(fields.get(GENERATION))
    elif GENERATION in fields:
        _refuse()
    return _Prepared(
        method,
        target,
        tuple(fields.items()),
        length,
        app,
        page,
        cookie,
        generation,
        query[1] if query is not None else None,
    )
