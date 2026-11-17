"""Explicit browser File-drop adapter; no host filesystem or prompt endpoint.

The native document drop contract is source-checked at:
  2.2.6 dc2ffcf8bf26f9e669b6aa22b95f50e12be15e06
  2.3.0 a6b15d33daf6ab7d35569f6d711fdb630b9f04e7
packages/app/src/composer/attachments/{attachments,destination}.ts,
composer/request.ts and runtime/persistence/drafts.ts are identical there.
packages/app/vite.pwa.ts excludes /auth/ navigations from its cached app shell.
This is standard DOM forwarding in an explicitly selected wrapper, not a
native public composer RPC, plugin/widget hook or an accepted host-browser run.
"""

import html
import os
from pathlib import Path
import re
import select
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

SUPPORTED = frozenset(("2.2.6", "2.3.0"))
# Stay inside native's existing service-worker navigation exclusion. Do not
# modify/unregister that worker or intercept ordinary native asset requests.
PATH = "/auth/oc-box-attachments"
SCRIPT = PATH + "/adapter.js"
MAX_HEADERS = 16384
MAX_REPLY_HEADERS = 65536


def session_link(ident):
    if (
        not isinstance(ident, str)
        or re.fullmatch(r"ses[A-Za-z0-9_-]{1,125}", ident) is None
    ):
        raise ValueError("session")
    return PATH + "?" + urlencode({"session": ident})


def selection(target):
    """Exactly one explicit native session; no owner/Home/first-row fallback."""
    if target == SCRIPT:
        return None
    path, separator, query = target.partition("?")
    if path != PATH or not separator:
        raise ValueError("route")
    pairs = parse_qsl(
        query, keep_blank_values=True, strict_parsing=True, max_num_fields=1
    )
    if len(pairs) != 1 or pairs[0][0] != "session":
        raise ValueError("route")
    ident = pairs[0][1]
    # Also refuse ambiguous/noncanonical encodings and dangling separators.
    if target != session_link(ident):
        raise ValueError("route")
    return ident


def matches(raw):
    """Reserve only this adapter namespace, never an arbitrary proxy target."""
    first = raw.split(b"\r\n", 1)[0].split(b" ")
    return len(first) >= 2 and first[1].startswith(PATH.encode())


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
    selection(target)
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


def page(native_route):
    route = html.escape(native_route, quote=True)
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>oc-box — native browser attachments</title>
<style>
html,body{margin:0;height:100%;font:14px system-ui;background:#17191d;color:#eef0f4}
body{display:flex;flex-direction:column}header{padding:10px 16px;border-bottom:1px solid #59616f}
p{margin:5px 0}button,input{font:inherit}a{color:#a6d4ff}
#status{min-height:1.4em}#drop{border:1px dashed #a6d4ff;padding:6px;display:inline-block}
iframe{flex:1;min-height:0;width:100%;border:0;background:#17191d}
details{max-width:90em}summary{cursor:pointer}
</style><script src="/auth/oc-box-attachments/adapter.js" defer></script></head>
<body><header>
<strong>oc-box attachment adapter — unmodified native OpenCode below</strong>
<p id="drop" tabindex="0">Drop PNG/JPEG/GIF/WebP/PDF here or into the native composer.
<input id="files" type="file" multiple
 accept="image/png,image/jpeg,image/gif,image/webp,application/pdf"
 aria-label="Choose image or PDF attachments"></p>
<p id="status" role="status" aria-live="polite">Checking native version…</p>
<details><summary>Attachment behavior and privacy</summary>
<p>Only browser-provided File bytes are forwarded. No host paths or URL fetching.
20 MiB per file, 40 MiB and eight files per drop. Signatures are checked, not a
full document safety scan. DOCX and other formats are not supported here.</p>
<p>The adapter strips competing URL/text flavors from real file drops.
No prompt/inference Send occurs until you press native Send; verify its thumbnails.
Your selected native model must support images/PDF for inline model input.
Native may otherwise upload to its container temporary directory as a path.</p>
<p>Native browser draft storage (including IndexedDB), container history and
provider delivery still apply. Clearing a thumbnail is not guaranteed erasure.
No automatic clipboard reads, prompt requests or retries are added.
Use the native UI directly for its unchanged non-media attachment behavior.</p>
</details><p><a id="direct" target="_blank" rel="noopener noreferrer" href=\""""
        + route
        + """\">Open native UI without adapter</a></p></header>
<iframe id="native" title="Native OpenCode workspace" data-route=\""""
        + route
        + """\"></iframe></body></html>"""
    ).encode("utf-8")


def response(stream, status, body=b"", *, head=False, javascript=False):
    reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 503: "Unavailable"}[
        status
    ]
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Content-Type: {'text/javascript' if javascript else 'text/html'}; charset=utf-8\r\n"
        "Connection: close\r\nCache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
        "X-Frame-Options: DENY\r\n"
        "Content-Security-Policy: default-src 'none'; script-src 'self'; "
        "style-src 'unsafe-inline'; frame-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\r\n\r\n"
    ).encode("ascii")
    stream.sendall(headers + (b"" if head else body))


def serve(stream, raw, child, origin, native_route):
    try:
        method, target, probe = request(raw, origin)
    except (ValueError, UnicodeError):
        response(stream, 403)
        return
    if not authorized(child, probe):
        response(stream, 401, head=method == "HEAD")
        return
    body = (
        (Path(__file__).with_name("web_attachments.js")).read_bytes()
        if target == SCRIPT
        else page(native_route(selection(target)))
    )
    response(stream, 200, body, head=method == "HEAD", javascript=target == SCRIPT)
