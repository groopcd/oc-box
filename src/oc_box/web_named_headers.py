"""Header admission for the dedicated named HTTPS sites, without a listener.

The HTTP server must bound the header read and pass the entire header block
with duplicates intact, as well as the actual method from its request line.
This module does not frame HTTP bodies or authorize a native route.
"""

import re
from urllib.parse import urlsplit

if __package__:
    from .web_tls import HTTPSOrigin
else:
    from web_tls import HTTPSOrigin

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
_MAX_FIELDS = 64
_MAX_HEADER_SIZE = 16384
_MAX_COOKIES = 32
_FORWARDED = frozenset(("authorization", "proxy-authorization", "forwarded"))
_COOKIE_NAMES = frozenset(
    ("__Host-ocbox-app", "__Host-ocbox-bootstrap", "__Host-ocbox-account")
)


class NamedRequestError(ValueError):
    def __init__(self):
        super().__init__("Named web request was not admitted.")


def _refuse():
    raise NamedRequestError()


def _fields(items, authority):
    if type(items) not in (list, tuple) or len(items) > _MAX_FIELDS:
        _refuse()
    fields, budget = {}, _MAX_HEADER_SIZE
    for pair in items:
        if type(pair) is not tuple or len(pair) != 2:
            _refuse()
        key, value = pair
        if (
            type(key) is not str
            or _HEADER_NAME.fullmatch(key) is None
            or type(value) is not str
        ):
            _refuse()
        budget -= len(key) + len(value) + 4
        name = key.lower()
        if (
            budget < 0
            or name in fields
            or name in _FORWARDED
            or name.startswith("x-forwarded-")
            or any(not 32 <= ord(char) < 127 for char in value)
        ):
            _refuse()
        fields[name] = value.strip(" ")
    if fields.get("host") != authority:
        _refuse()
    return fields


def _cookie(header, name):
    if header is None:
        _refuse()
    cookies = header.split(";")
    if len(cookies) > _MAX_COOKIES:
        _refuse()
    found = None
    for part in cookies:
        key, _, value = part.strip(" ").partition("=")
        if key == name:
            if found is not None or _TOKEN.fullmatch(value) is None:
                _refuse()
            found = value
    if found is None:
        _refuse()
    return found


def admit_headers(
    *,
    origin,
    headers,
    method,
    actual_tls,
    websocket=False,
    form_origin=None,
    cookie_name=None,
):
    """Check exact site and context before any named app or protocol dispatch.

    GET/HEAD without Origin are allowed only for same-origin, browser-direct
    navigation/fetch; unsafe requests and WebSocket handshakes require an
    exact Origin. The only cross-site exception is an explicitly configured
    POST-only handoff endpoint, not a route forwarded to native OpenCode.
    Return only a selected private cookie value, or None when none was asked
    for. Do not return or forward arbitrary request cookies.
    """
    if (
        type(origin) is not HTTPSOrigin
        or actual_tls is not True
        or type(websocket) is not bool
        or method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
        or (websocket and method != "GET")
        or (cookie_name is not None and cookie_name not in _COOKIE_NAMES)
    ):
        _refuse()
    cross = form_origin is not None
    if cross and (
        type(form_origin) is not HTTPSOrigin
        or form_origin.hostname == origin.hostname
        or method != "POST"
        or websocket
        or cookie_name is not None
    ):
        _refuse()
    fields = _fields(headers, origin.value.removeprefix("https://"))
    expected = form_origin.value if cross else origin.value
    supplied = fields.get("origin")
    safe = method in ("GET", "HEAD") and not websocket
    if (supplied is None and not safe) or supplied not in (None, expected):
        _refuse()
    site = fields.get("sec-fetch-site")
    if cross:
        if (
            site != "cross-site"
            or fields.get("sec-fetch-mode") != "navigate"
            or fields.get("sec-fetch-dest") != "document"
            or "cookie" in fields
        ):
            _refuse()
    elif site not in (None, "same-origin", "none") or (
        supplied is None and site is None
    ):
        _refuse()
    if websocket != (fields.get("upgrade", "").lower() == "websocket"):
        _refuse()
    connection = {
        item.strip().lower() for item in fields.get("connection", "").split(",")
    }
    if websocket != ("upgrade" in connection):
        _refuse()
    if "upgrade" in fields and not websocket:
        _refuse()
    if (referer := fields.get("referer")) is not None:
        try:
            parsed = urlsplit(referer)
            if parsed.scheme + "://" + parsed.netloc != expected:
                _refuse()
        except ValueError:
            _refuse()
    return _cookie(fields.get("cookie"), cookie_name) if cookie_name else None
