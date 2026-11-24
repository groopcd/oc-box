"""Explicit browser File-drop adapter; no host filesystem or prompt endpoint.

The native document drop contract is source-checked at:
  2.2.6 dc2ffcf8bf26f9e669b6aa22b95f50e12be15e06
  2.3.0 a6b15d33daf6ab7d35569f6d711fdb630b9f04e7
packages/app/src/composer/attachments/{attachments,destination}.ts,
composer/request.ts and runtime/persistence/drafts.ts are identical there.
The exact versions below additionally passed Chromium chooser/drop, two
isolated native drafts and no automatic Send; see smoke_web_attachments_browser.py.
Native's service worker excludes /auth/ from cached app-shell navigations.
This is standard DOM forwarding, not a native public composer RPC, plugin/widget
hook or external-host acceptance. Native API/DOM capability alone is not proof.
"""

import os
from pathlib import Path
import re
import select
import time
from urllib.parse import urlsplit

SUPPORTED = frozenset(("2.2.6", "2.2.7", "2.3.0", "2.3.2", "2.4.1"))
UNVERIFIED_RELEASE = (
    "The browser attachment adapter is not verified for this native OpenCode release. "
    "Use 'oc-box web' without --attachments, or Ctrl+Y in the oc-box TUI with "
    "--host-clipboard where supported."
)
# Stay inside native's existing service-worker navigation exclusion. Do not
# modify/unregister that worker or intercept ordinary native asset requests.
SCRIPT = "/auth/oc-box-attachments.js"
MAX_HEADERS = 16384
MAX_REPLY_HEADERS = 65536


def supports_release(version):
    return isinstance(version, str) and version in SUPPORTED


def matches(raw):
    """Reserve only this adapter namespace, never an arbitrary proxy target."""
    first = raw.split(b"\r\n", 1)[0].split(b" ")
    return len(first) >= 2 and first[1].startswith(SCRIPT.encode())


def request(raw, origin):
    """Strict, bodyless, same-origin GET/HEAD; return only native auth headers."""
    if len(raw) > MAX_HEADERS or not raw.endswith(b"\r\n\r\n"):
        raise ValueError("request")
    lines = raw[:-4].decode("ascii").split("\r\n")
    first = lines.pop(0).split(" ")
    if len(first) != 3 or first[2] != "HTTP/1.1":
        raise ValueError("request")
    method, target, _ = first
    if method not in ("GET", "HEAD"):
        raise ValueError("method")
    if target != SCRIPT:
        raise ValueError("route")
    headers = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9-]+", key):
            raise ValueError("headers")
        key, value = key.lower(), value.strip(" \t")
        if key in headers or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("headers")
        headers[key] = value
    authority = urlsplit(origin).netloc
    if headers.get("host") != authority:
        raise ValueError("origin")
    if "origin" in headers and headers["origin"] != origin:
        raise ValueError("origin")
    if "referer" in headers:
        referer = urlsplit(headers["referer"])
        if referer.scheme + "://" + referer.netloc != origin:
            raise ValueError("origin")
    if headers.get("sec-fetch-site", "none") not in ("none", "same-origin"):
        raise ValueError("origin")
    if (
        headers.get("content-length", "0") != "0"
        or "transfer-encoding" in headers
        or "upgrade" in headers
    ):
        raise ValueError("body")
    # Native owns cookie/signature/expiry validation. Never substitute the
    # controller password for the browser's own credentials.
    credentials = b"".join(
        (key + ": " + headers[key] + "\r\n").encode("ascii")
        for key in ("cookie", "authorization")
        if key in headers
    )
    probe = (
        b"GET /api/info HTTP/1.1\r\nHost: "
        + authority.encode("ascii")
        + b"\r\nOrigin: "
        + origin.encode("ascii")
        + b"\r\nConnection: close\r\n"
        + credentials
        + b"\r\n"
    )
    return method, target, probe


def authorized(child, probe, *, timeout=5):
    """Use the existing pinned relay for one bounded native authorization read."""
    child.stdin.write(probe)
    child.stdin.flush()
    deadline = time.monotonic() + timeout
    response = bytearray()
    while b"\r\n\r\n" not in response:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
            return False
        chunk = os.read(
            child.stdout.fileno(), min(4096, MAX_REPLY_HEADERS - len(response))
        )
        if not chunk:
            return False
        response.extend(chunk)
        if len(response) >= MAX_REPLY_HEADERS:
            return False
    return (
        re.fullmatch(rb"HTTP/1\.[01] 200(?: [^\r\n]*)?", response.split(b"\r\n", 1)[0])
        is not None
    )


def response(stream, status, body=b"", *, head=False, javascript=False):
    reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 503: "Unavailable"}[
        status
    ]
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Content-Type: {'text/javascript' if javascript else 'text/plain'}; charset=utf-8\r\n"
        "Connection: close\r\nCache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
        "X-Frame-Options: DENY\r\n"
        "Content-Security-Policy: default-src 'none'; script-src 'self'; "
        "style-src 'self'; frame-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\r\n\r\n"
    ).encode("ascii")
    stream.sendall(headers + (b"" if head else body))


def serve(stream, raw, child, origin):
    try:
        method, _target, probe = request(raw, origin)
    except (ValueError, UnicodeError):
        response(stream, 403)
        return
    if not authorized(child, probe):
        response(stream, 401, head=method == "HEAD")
        return
    body = Path(__file__).with_name("web_attachments.js").read_bytes()
    response(stream, 200, body, head=method == "HEAD", javascript=True)
