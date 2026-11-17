"""Authenticated open-TUI-session index, not native browser tab manipulation.

Native 2.2.6/2.3.0 web tabs are client-local (app shell/tabs/tabs.tsx).
The public /server/:serverKey/session/:id route opens one session, not a tab
inventory. Keep that app unchanged and offer explicit navigation here instead.
"""

import base64
from html import escape
import os
import select
import subprocess
import time

if __package__:
    from . import native_tabs, opencode_v2_io
else:
    import native_tabs
    import opencode_v2_io

# Native 2.2.6/2.3.0 app/vite.pwa.ts excludes /auth/ from its navigation
# fallback. A path outside that namespace can be swallowed by an installed SW.
INDEX = "/auth/oc-box-sessions"


def native_session_route(url, ident):
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return "/server/" + encoded + "/session/" + ident


def open_sessions(box, data, connection):
    """List verified native membership, not automatic-resume/history candidates.

    An idle tab remains open. Activity is deliberately not consulted. The
    native saved scope may contain worktree/moved, archived or child IDs.
    Every listed ID must still exist in THIS private service; no other service
    or host history is queried and no missing membership is guessed.
    """
    if connection.get("workspace") != data["id"]:
        raise box.BoxError("Open web sessions could not be verified.")
    box.workspace_service.validate_endpoint(connection)
    snapshot = native_tabs.inspect(box, data, connection=connection)
    if snapshot.status not in ("valid", "missing"):
        raise box.BoxError("Open web sessions could not be verified.")
    if snapshot.status == "missing" or not snapshot.tabs:
        return [], snapshot.status
    inventory = box.workspace_service.web_session_catalog(
        box, data, connection, snapshot.tabs
    )
    if native_tabs.inspect(box, data, connection=connection) != snapshot:
        raise box.BoxError("Open web sessions changed; refresh the session index.")
    if not isinstance(inventory, list):
        raise box.BoxError("Open web sessions could not be verified.")
    verified = {}
    for info in inventory:
        opencode_v2_io.web_session_info(info)
        if info["id"] in verified:
            raise box.BoxError("Open web sessions could not be verified.")
        verified[info["id"]] = info
    return [
        verified[ident] for ident in snapshot.tabs if ident in verified
    ], snapshot.status


def home_navigation():
    """Explicit native UI navigation, never an inferred TUI/session target."""
    return (
        '<p><a href="/" target="_blank" rel="noopener noreferrer">'
        "Open native OpenCode Home</a> to explicitly create or open a browser "
        "session. Browser-only tabs remain native-client local; they do not "
        "populate this TUI list.</p>"
    )


def document(url, sessions, status):
    items = []
    for info in sessions:
        ident = info["id"]
        title = info.get("title")
        title = title[:256] if isinstance(title, str) and title else ident
        items.append(
            '<li><a href="'
            + escape(native_session_route(url, ident), quote=True)
            + '" target="_blank" rel="noopener noreferrer">'
            + escape(title)
            + "</a> <small>"
            + escape(ident)
            + "</small></li>"
        )
    heading = "Open TUI sessions"
    note = (
        "Open TUI tabs, including idle sessions, in native tab order."
        if status == "valid"
        else "Saved TUI tab membership is unavailable. No current session or "
        "historical session is guessed."
    )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>oc-box — workspace sessions</title><body><main><h1>"
        + heading
        + "</h1><p>"
        + note
        + "</p><p>This index is for this private workspace only. Links open the "
        "unmodified native web app in separate browser tabs. Native web tabs are "
        "independent of TUI tabs; this page does not synchronize their membership."
        "</p>"
        + home_navigation()
        + '<p><a href="'
        + INDEX
        + '">Refresh session list</a></p>'
        + (
            "<ol>" + "".join(items) + "</ol>"
            if items
            else "<p>No open session is listed.</p>"
        )
        + "</main></body></html>"
    ).encode("utf-8", errors="replace")


def error_document():
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>oc-box — workspace sessions unavailable</title><body><main>"
        "<h1>Open TUI sessions unavailable</h1><p>Open sessions could not be "
        'verified. <a href="'
        + INDEX
        + '">Refresh session list</a> to retry.</p>'
        + home_navigation()
        + "</main></body></html>"
    ).encode()


def _reply(stream, status, body, content_type="text/plain; charset=utf-8"):
    stream.sendall(
        (
            "HTTP/1.1 "
            + status
            + "\r\nContent-Type: "
            + content_type
            + "\r\nContent-Length: "
            + str(len(body))
            + "\r\nConnection: close\r\nCache-Control: no-store\r\n"
            "Referrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n"
            "Content-Security-Policy: default-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'\r\n\r\n"
        ).encode()
        + body
    )


def _authenticated(child, headers, authority):
    """Ask the SAME native service to validate the caller's native credentials.

    Never substitute the owner's password: loopback alone is not authorization.
    Preserve Host/Origin for native port-scoped cookie and same-origin checks.
    Forward only those headers and the caller's Cookie/Authorization.
    Discard the body and never render, save or log authentication material.
    """
    credentials = []
    seen = set()
    for line in headers.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        name = name.lower()
        if not separator or name in seen:
            return False
        seen.add(name)
        if name == b"host" and value.strip() != authority.encode():
            return False
        if name in (b"host", b"origin", b"cookie", b"authorization"):
            credentials.append(name + b":" + value)
    if b"host" not in seen:
        return False
    child.stdin.write(
        b"GET /api/info HTTP/1.1\r\nConnection: close\r\n"
        + b"".join(line + b"\r\n" for line in credentials)
        + b"\r\n"
    )
    child.stdin.flush()
    response = bytearray()
    deadline = time.monotonic() + 5
    while b"\r\n\r\n" not in response:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(response) >= 65536:
            return False
        if not select.select([child.stdout], [], [], remaining)[0]:
            return False
        chunk = os.read(child.stdout.fileno(), min(4096, 65536 - len(response)))
        if not chunk:
            return False
        response.extend(chunk)
    return response.split(b"\r\n", 1)[0].split(b" ")[:2] in (
        [b"HTTP/1.1", b"200"],
        [b"HTTP/1.0", b"200"],
    )


def respond(gateway, stream, child, request):
    """Intercept only the oc-box index; all native assets/API/WS pass untouched."""
    header, _, body = request.partition(b"\r\n\r\n")
    first = header.split(b"\r\n", 1)[0].split(b" ")
    if len(first) != 3 or first[1].split(b"?", 1)[0] != INDEX.encode():
        return False
    if first[0] != b"GET" or body:
        _reply(stream, "400 Bad Request", b"Invalid session index request.")
        return True
    if not _authenticated(child, header, gateway.url.removeprefix("http://")):
        _reply(
            stream, "401 Unauthorized", b"Use the native one-use pairing link first."
        )
        return True
    try:
        sessions, status = open_sessions(gateway.box, gateway.data, gateway.connection)
    except (
        gateway.box.BoxError,
        native_tabs.TabError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        _reply(
            stream,
            "503 Service Unavailable",
            error_document(),
            "text/html; charset=utf-8",
        )
        return True
    _reply(
        stream,
        "200 OK",
        document(gateway.url, sessions, status),
        "text/html; charset=utf-8",
    )
    return True
