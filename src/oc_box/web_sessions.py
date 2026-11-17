"""Authenticated open-TUI-session index, not native browser tab manipulation.

Native 2.2.6/2.3.0 web tabs are client-local (app shell/tabs/tabs.tsx).
The public /server/:serverKey/session/:id route opens one session, not a tab
inventory. Keep that app unchanged and offer explicit navigation here instead.
"""

import base64
from html import escape
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

if __package__:
    from . import native_tabs, opencode_v2_io
else:
    import native_tabs
    import opencode_v2_io

# Native 2.2.6/2.3.0 app/vite.pwa.ts excludes /auth/ from its navigation
# fallback. A path outside that namespace can be swallowed by an installed SW.
INDEX = "/auth/oc-box-sessions"
SCRIPT = INDEX + ".js"
STYLE = INDEX + ".css"
CATALOG = INDEX + "/catalog"
SELECTION = INDEX + "/selection"


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


def explicit_session(box, data, connection, ident):
    """Explicit selection only; existing identity in this private DB, not history guessing."""
    if not box.valid_session_id(ident):
        raise box.BoxError("Invalid explicit web session ID.")
    found = box.workspace_service.web_session_catalog(box, data, connection, [ident])
    if len(found) != 1 or opencode_v2_io.web_session_info(found[0])["id"] != ident:
        raise box.BoxError(
            "Explicit web session is unavailable in this private service."
        )
    return found[0]


def resolve_initial_selection(
    members,
    *,
    requested=None,
    override=None,
    declared=None,
    report=None,
    report_requested=False,
):
    """Selection seam: no claim of native focus, order/time/activity are not evidence."""
    if override is not None:
        return override, "Explicit --session selection; not verified native focus"
    if requested is not None:
        return requested, "URL-selected conversation; not verified native focus"
    if report_requested:
        # A future helper adapter must explicitly normalize verified evidence.
        # Do not infer proof from timestamps, ordered tabs, or a stale owner.
        if (
            isinstance(report, dict)
            and report.get("status") == "verified"
            and isinstance(report.get("selectedSessionID"), str)
            and report.get("selectedSessionID") in members
            and isinstance(report.get("source"), str)
        ):
            return report[
                "selectedSessionID"
            ], "Verified TUI selection report: " + report["source"][:128]
        return None, "Native TUI selection report unavailable; choose a tab"
    if declared in members:
        return declared, "Launcher-declared selection; not verified native focus"
    return None, "Choose a tab; native TUI focus is not verified"


def catalog(url, sessions, status):
    return {
        "status": status,
        "nativePrefix": native_session_route(url, "ses_placeholder").rsplit("/", 1)[0]
        + "/",
        "sessions": [
            {
                "id": info["id"],
                "title": (info.get("title") or info["id"])[:256]
                if isinstance(info.get("title"), str)
                else info["id"],
                "route": native_session_route(url, info["id"]),
            }
            for info in sessions
        ],
    }


def initial_state(gateway, sessions, status, requested=None):
    state = catalog(gateway.url, sessions, status)
    members = {info["id"]: info for info in state["sessions"]}
    reader = getattr(gateway, "selection_snapshot_reader", None)
    report = None
    override = gateway.connection.get("web_session_override")
    if reader is not None and override is None and requested is None:
        try:
            report = reader(gateway.box, gateway.data, gateway.connection)
        except Exception:
            # Fixed unavailable state, never private helper errors or old-owner
            # fallback. Explicit user selection remains independent.
            report = None
    ident, origin = resolve_initial_selection(
        members,
        requested=requested,
        override=override,
        declared=gateway.connection.get("session"),
        report=report,
        report_requested=reader is not None,
    )
    selected = None
    if ident is not None:
        selected = members.get(ident)
        if selected is None:
            info = explicit_session(
                gateway.box, gateway.data, gateway.connection, ident
            )
            selected = catalog(gateway.url, [info], status)["sessions"][0]
        selected = {**selected, "origin": origin, "member": ident in members}
    return {**state, "selection": selected, "selectionNotice": origin}


def document(url, sessions, status, state=None, addon_scripts=()):
    if (
        not isinstance(addon_scripts, tuple)
        or len(addon_scripts) > 1
        or any(script != "/auth/oc-box-attachments.js" for script in addon_scripts)
    ):
        raise ValueError("Invalid trusted shell addon.")
    state = state or {
        **catalog(url, sessions, status),
        "selection": None,
        "selectionNotice": "Choose a tab; native TUI focus is not verified",
    }
    selected = state["selection"]
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
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>oc-box — workspace sessions</title><link rel="stylesheet" href="'
        + STYLE
        + '"><script defer src="'
        + SCRIPT
        + '"></script>'
        + "".join(
            '<script defer src="' + script + '"></script>' for script in addon_scripts
        )
        + "</head><body>"
        '<main id="web-shell" data-bootstrap="'
        + escape(json.dumps(state, ensure_ascii=True), quote=True)
        + '"><header><h1>'
        + heading
        + "</h1><p>"
        + note
        + "</p><p>This shell is for this private workspace only. Its tabstrip "
        "mirrors TUI membership; the single frame displays the unmodified native "
        "web app. Native web tabs are "
        "independent of TUI tabs; this page does not synchronize their membership."
        "</p>"
        + home_navigation()
        + '<p><a href="'
        + INDEX
        + '">Reload shell</a> (reload may interrupt a draft). '
        '<button id="refresh-tabs" type="button">Refresh TUI tabs without reloading conversation</button></p>'
        '<p id="catalog-status" role="status"></p><p id="selection-status">'
        + escape(state["selectionNotice"])
        + "</p>"
        '<div id="session-tabs" role="tablist" aria-label="Open native TUI tabs"></div>'
        '<div id="retained-selection"></div><div id="session-controls"></div>'
        '<p id="addon-status" role="status"></p></header>'
        '<section id="no-selection"' + (" hidden" if selected else "") + ">"
        "<p>Select a TUI tab, use --session ID, or open native Home. No conversation is guessed.</p>"
        '</section><iframe id="native" name="oc-box-native-session" '
        'title="Native OpenCode conversation" referrerpolicy="no-referrer"'
        + (
            ' src="' + escape(selected["route"], quote=True) + '"'
            if selected
            else " hidden"
        )
        + '></iframe><details id="fallback-links"><summary>Direct native conversation links (initial verified snapshot)</summary>'
        + (
            "<ol>" + "".join(items) + "</ol>"
            if items
            else "<p>No open session is listed.</p>"
        )
        + "</details></main></body></html>"
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
            "Content-Security-Policy: default-src 'none'; script-src 'self'; "
            "style-src 'self'; frame-src 'self'; connect-src 'self'; base-uri 'none'; "
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
    if len(first) != 3:
        return False
    if first[1].split(b"?", 1)[0] not in tuple(
        path.encode() for path in (INDEX, SCRIPT, STYLE, CATALOG, SELECTION)
    ):
        return False
    target = urlsplit(first[1].decode("ascii"))
    if first[0] != b"GET" or body or len(request) > 16384:
        _reply(stream, "400 Bad Request", b"Invalid session index request.")
        return True
    fields = {}
    for line in header.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        name, value = name.lower(), value.strip()
        if (
            not separator
            or name in fields
            or re.fullmatch(rb"[A-Za-z0-9-]+", name) is None
            or any(byte < 32 or byte == 127 for byte in value)
        ):
            _reply(stream, "401 Unauthorized", b"Invalid first-party request.")
            return True
        fields[name] = value
    # Native cookie validation below remains authoritative. Reject cross-origin
    # browser fetches even when localhost ports are considered same-site.
    referer = fields.get(b"referer")
    if (
        fields.get(b"origin", gateway.url.encode()) != gateway.url.encode()
        or fields.get(b"sec-fetch-site", b"none") not in (b"none", b"same-origin")
        or fields.get(b"content-length", b"0") != b"0"
        or b"transfer-encoding" in fields
        or b"upgrade" in fields
        or (
            referer is not None
            and (lambda u: u.scheme + "://" + u.netloc)(
                urlsplit(referer.decode("ascii"))
            )
            != gateway.url
        )
    ):
        _reply(stream, "401 Unauthorized", b"Invalid first-party request origin.")
        return True
    if not _authenticated(child, header, gateway.url.removeprefix("http://")):
        _reply(
            stream, "401 Unauthorized", b"Use the native one-use pairing link first."
        )
        return True
    try:
        query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
        if set(query) - {"session"} or (
            "session" in query and len(query["session"]) != 1
        ):
            raise ValueError("Invalid shell query.")
        if target.path in (SCRIPT, STYLE):
            if query:
                raise ValueError("Invalid asset query.")
            name, content_type = (
                ("web_sessions.js", "text/javascript; charset=utf-8")
                if target.path == SCRIPT
                else ("web_sessions.css", "text/css; charset=utf-8")
            )
            _reply(
                stream,
                "200 OK",
                (Path(__file__).parent / name).read_bytes(),
                content_type,
            )
            return True
        if target.path == SELECTION:
            if set(query) != {"session"}:
                raise ValueError("Missing selected session.")
            info = explicit_session(
                gateway.box, gateway.data, gateway.connection, query["session"][0]
            )
            _reply(
                stream,
                "200 OK",
                json.dumps(
                    catalog(gateway.url, [info], "explicit")["sessions"][0],
                    ensure_ascii=True,
                ).encode(),
                "application/json",
            )
            return True
        try:
            sessions, status = open_sessions(
                gateway.box, gateway.data, gateway.connection
            )
        except (
            gateway.box.BoxError,
            native_tabs.TabError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ):
            # Explicit existing private IDs do not depend on readable TUI state.
            # No automatic/default target is admitted on this fallback path.
            if target.path != INDEX or (
                gateway.connection.get("web_session_override") is None
                and "session" not in query
            ):
                raise
            sessions, status = [], "unavailable"
        if target.path == CATALOG:
            _reply(
                stream,
                "200 OK",
                json.dumps(
                    catalog(gateway.url, sessions, status), ensure_ascii=True
                ).encode(),
                "application/json",
            )
            return True
        state = initial_state(
            gateway, sessions, status, query.get("session", [None])[0]
        )
        body = document(
            gateway.url,
            sessions,
            status,
            state,
            addon_scripts=getattr(gateway, "shell_addon_scripts", ()),
        )
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
        body,
        "text/html; charset=utf-8",
    )
    return True
