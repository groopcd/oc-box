"""Internal native pairing for the dedicated local HTTPS workspace origin.

The caller must have independently authorized the sign-in and hold the verified
workspace_service.attach lease throughout issuance and use. This is not browser
admission, workspace discovery, a revocation mechanism, or a request handler.
It opens no listener. No native cookie or pairing link goes to the browser.
"""

import base64
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .web_admission import WebBinding
    from .web_local_origins import WORKSPACE_HOST
    from .web_tls import HTTPSOrigin
elif __name__ != "__main__":
    from web_admission import WebBinding
    from web_local_origins import WORKSPACE_HOST
    from web_tls import HTTPSOrigin


_VERSION = "2.3.2"
_HOST_TIMEOUT = 8
_WORKER_TIMEOUT = 5
_MAX_HEAD = 8192
_MAX_BODY = 8192
_CODE = re.compile(r"[A-Za-z0-9_-]{22}")
_VALUE = re.compile(r"[A-Za-z0-9_-]{10}\.[A-Za-z0-9_-]{43}")
_VALUE_SIZE = 54
_ATTRIBUTES = {"max-age=2592000", "path=/", "httponly", "samesite=lax"}


class NativePairError(ValueError):
    """Fixed refusal without native output, endpoint, or credential details."""

    def __init__(self):
        super().__init__("Native workspace pairing could not be verified.")


def _refuse():
    raise NativePairError() from None


def _authority(origin):
    if (
        type(origin) is not HTTPSOrigin
        or origin.hostname != WORKSPACE_HOST
        or type(origin.port) is not int
        or (origin.port != 443 and not 1024 <= origin.port <= 65535)
    ):
        _refuse()
    return origin.value.removeprefix("https://")


def _port(endpoint):
    match = (
        re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?", endpoint)
        if type(endpoint) is str and len(endpoint) <= 23
        else None
    )
    if match is None or (port := int(match[1])) > 65535:
        _refuse()
    return port


def _cookie_name(authority):
    # 2.3.2 names this from the forwarded authority, not the TCP peer port.
    _, _, port = authority.partition(":")
    return "opencode_session" + ("_" + port if port else "")


class NativeCookie:
    """Private upstream credential, not a browser cookie or an admission grant."""

    __slots__ = ("_binding", "_scope", "_value", "_owner_current")

    def __init__(self, binding, endpoint, authority, value, *, owner_current):
        if (
            type(binding) is not WebBinding
            or binding.origin != "https://" + authority
            or not callable(owner_current)
            or type(value) is not str
            or _VALUE.fullmatch(value) is None
        ):
            _refuse()
        self._binding = binding
        self._scope = endpoint, authority
        self._value = value
        self._owner_current = owner_current

    def upstream_cookie(self, *, binding, endpoint, origin):
        """Return only the Cookie value for this trusted, still-leased upstream.

        Do not send it in a browser response, merge it with browser cookies,
        rebind it, log it or persist it. It has native workspace-wide authority.
        """
        authority = _authority(origin)
        if (
            type(binding) is not WebBinding
            or binding != self._binding
            or self._scope != (endpoint, authority)
            or not _owner_matches(self._owner_current, binding)
        ):
            _refuse()
        return _cookie_name(authority) + "=" + self._value


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _refuse()
    return remaining


def _reply(raw):
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator or len(head) > _MAX_HEAD or len(body) > _MAX_BODY:
        _refuse()
    status, *fields = head.split(b"\r\n")
    match = re.fullmatch(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [\x20-\x7e]*)?", status)
    if match is None:
        _refuse()
    headers = {}
    for field in fields:
        name, colon, value = field.partition(b":")
        if (
            not colon
            or re.fullmatch(rb"[A-Za-z0-9-]+", name) is None
            or re.fullmatch(rb"[\t\x20-\x7e]*", value) is None
        ):
            _refuse()
        headers.setdefault(name.lower().decode("ascii"), []).append(
            value.strip(b" \t").decode("ascii")
        )
    return int(match[1]), headers, body


def _exchange(
    port, authority, deadline, route, *, method="GET", auth=None, cookie=None
):
    # Standalone worker inside the providerless Podman exec bridge. Neither
    # Host nor Origin changes the literal loopback TCP destination. No client
    # headers, proxy discovery, HTTP library logging, redirects or retries.
    fields = [
        f"{method} {route} HTTP/1.1",
        f"Host: {authority}",
        f"Origin: https://{authority}",
        "Accept: text/html"
        if route.startswith("/auth/")
        else "Accept: application/json",
        "Accept-Encoding: identity",
        "Connection: close",
    ]
    if method == "POST":
        fields.append("Content-Length: 0")
    if auth is not None:
        fields.append("Authorization: " + auth)
    if cookie is not None:
        fields.append("Cookie: " + cookie)
    limit = _MAX_HEAD + 4 + _MAX_BODY
    raw = bytearray()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
        peer.settimeout(_remaining(deadline))
        peer.connect(("127.0.0.1", port))
        peer.settimeout(_remaining(deadline))
        peer.sendall(("\r\n".join(fields) + "\r\n\r\n").encode("ascii"))
        while True:
            peer.settimeout(_remaining(deadline))
            chunk = peer.recv(min(4096, limit + 1 - len(raw)))
            if not chunk:
                return _reply(raw)
            raw.extend(chunk)
            if len(raw) > limit:
                _refuse()


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        _refuse()
    return result


def _json_reply(reply):
    status, headers, body = reply
    if (
        status != 200
        or not body
        or "set-cookie" in headers
        or "set-cookie2" in headers
        or "location" in headers
        or "transfer-encoding" in headers
        or headers.get("content-encoding", []) not in ([], ["identity"])
        or headers.get("content-type") != ["application/json"]
        or headers.get("content-length") != [str(len(body))]
    ):
        _refuse()
    result = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique,
        parse_constant=lambda _: _refuse(),
    )
    if type(result) is not dict:
        _refuse()
    return result


def _identity(reply, port):
    info = _json_reply(reply)
    if (
        info.get("version") != _VERSION
        or type(info.get("pid")) is not int
        or info["pid"] <= 0
        or type(info.get("urls")) is not list
        or f"http://127.0.0.1:{port}" not in info["urls"]
    ):
        _refuse()
    return info


def _parse_cookie(reply, authority):
    status, headers, body = reply
    cookies = headers.get("set-cookie", [])
    # Audited native reply is an empty, chunked 302 (also accept explicit zero
    # length). There is nothing to forward or follow, even on success.
    if (
        status != 302
        or headers.get("location") != ["/"]
        or len(cookies) != 1
        or "set-cookie2" in headers
        or "content-encoding" in headers
        or (headers.get("transfer-encoding"), headers.get("content-length"), body)
        not in ((["chunked"], None, b"0\r\n\r\n"), (None, ["0"], b""))
    ):
        _refuse()
    pair, *attributes = [part.strip(" \t") for part in cookies[0].split(";")]
    name, equals, value = pair.partition("=")
    if (
        not equals
        or name != _cookie_name(authority)
        or _VALUE.fullmatch(value) is None
        or len(attributes) != len(_ATTRIBUTES)
        or {attribute.lower() for attribute in attributes} != _ATTRIBUTES
    ):
        _refuse()
    return value


def _worker(endpoint, authority):
    port = _port(endpoint)
    if re.fullmatch(r"[a-z0-9.-]{1,253}(?::[1-9][0-9]{0,4})?", authority) is None:
        _refuse()
    password = os.environ.pop("OPENCODE_PASSWORD", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", password) is None:
        _refuse()
    auth = "Basic " + base64.b64encode(("opencode:" + password).encode("ascii")).decode(
        "ascii"
    )
    deadline = time.monotonic() + _WORKER_TIMEOUT

    def fetch(route, **options):
        return _exchange(port, authority, deadline, route, **options)

    status, headers, _ = fetch("/api/info")
    if status != 401 or {"set-cookie", "set-cookie2", "location"} & headers.keys():
        _refuse()
    before = _identity(fetch("/api/info", auth=auth), port)
    minted = _json_reply(fetch("/api/pair", method="POST", auth=auth))
    if (
        minted.keys() != {"code", "expires_in"}
        or type(code := minted["code"]) is not str
        or _CODE.fullmatch(code) is None
        or type(minted["expires_in"]) is not int
        or minted["expires_in"] != 300
    ):
        _refuse()
    value = _parse_cookie(fetch("/auth/connect/" + code), authority)
    if any(secret in value for secret in (code, password, auth[6:])):
        _refuse()
    paired = _identity(
        fetch("/api/info", cookie=_cookie_name(authority) + "=" + value), port
    )
    if paired != before:
        _refuse()
    sys.stdout.buffer.write(value.encode("ascii"))


def _read_bridge(command):
    # Do not use an unbounded capture or include stdout/stderr in diagnostics.
    deadline = time.monotonic() + _HOST_TIMEOUT
    process = subprocess.Popen(
        command,
        env=command.environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        output = bytearray()
        with process.stdout as stream:
            while True:
                if not select.select([stream], [], [], _remaining(deadline))[0]:
                    _refuse()
                chunk = os.read(stream.fileno(), _VALUE_SIZE + 1 - len(output))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _VALUE_SIZE:
                    _refuse()
        if (
            process.wait(timeout=_remaining(deadline)) != 0
            or len(output) != _VALUE_SIZE
        ):
            _refuse()
        return output.decode("ascii")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def _owner_matches(owner_current, binding):
    try:
        return owner_current(binding) is True
    except Exception:  # noqa: BLE001 - lease check errors fail closed with no raw diagnostics.
        return False


def pair_cookie(box, data, connection, *, origin, binding, owner_current):
    """Mint once on an already-held, verified 2.3.2 private workspace lease.

    Supply the origin from the trusted local TLS profile, never Host/Origin
    request headers. The only subprocess output accepted is the verified
    native token, carried over a private bounded pipe. A refusal is not proof
    that native minting had no side effects. This helper does not retry.
    """
    try:
        authority = _authority(origin)
        if (
            type(data) is not dict
            or type(connection) is not dict
            or type(workspace := data.get("id")) is not str
            or not workspace
            or connection.get("workspace") != workspace
            or connection.get("version") != _VERSION
            or data.get("opencode_version") != _VERSION
            or type(binding) is not WebBinding
            or binding.workspace_id != workspace
            or binding.origin != origin.value
            or not _owner_matches(owner_current, binding)
        ):
            _refuse()
        endpoint = connection.get("url")
        _port(endpoint)
        box.workspace_service.validate_endpoint(connection)
        command = box.workspace_service.client_command(
            box,
            data,
            connection,
            [
                "python3",
                "-I",
                "-S",
                "-B",
                "-c",
                Path(__file__).read_text(encoding="utf-8"),
                endpoint,
                authority,
            ],
        )
        value = _read_bridge(command)
        if not _owner_matches(owner_current, binding):
            _refuse()
        return NativeCookie(
            binding, endpoint, authority, value, owner_current=owner_current
        )
    except (
        box.BoxError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        pass
    _refuse()


if __name__ == "__main__":
    try:
        _worker(*sys.argv[1:])
    except Exception:  # Worker refusal must not print native data or tracebacks.
        sys.exit(1)
