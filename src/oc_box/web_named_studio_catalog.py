"""Unserved recent-session metadata for one named Studio workspace."""

import base64
from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import re
from time import monotonic
from urllib.parse import quote, urlencode

if __package__:
    from . import (
        web_app_ingress,
        web_named_headers,
        web_named_owner,
        web_named_relay,
        web_named_transport,
        web_native_pair,
        web_studio_conversation as studio,
        web_studio_send,
    )
else:
    import web_app_ingress
    import web_named_headers
    import web_named_owner
    import web_named_relay
    import web_named_transport
    import web_native_pair
    import web_studio_conversation as studio
    import web_studio_send


PATH = "/oc-box/studio/catalog"
RECENT_LIMIT = 16
MAX_TITLE_BYTES = 160
_INFO = "/api/info"
_LOCATION = "/api/location"
_PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _refuse():
    raise web_named_headers.NamedRequestError() from None


def _list_path(directory):
    return "/api/session?" + urlencode(
        (("directory", directory), ("order", "desc"), ("limit", RECENT_LIMIT)),
        quote_via=quote,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _LocationEvidence:
    info: dict
    location: dict
    project_id: str
    subpath: str | None


def _snapshot(get, owner, directory, *, closing=False):
    info = get(_INFO)
    web_native_pair.named_native_info(info, owner.version, owner.endpoint)
    location = get(_LOCATION, closing=closing)
    if (
        type(location) is not dict
        or location.get("directory") != directory
        or type(project := location.get("project")) is not dict
        or type(project_id := project.get("id")) is not str
        or _PROJECT_ID.fullmatch(project_id) is None
    ):
        _refuse()
    root = PurePosixPath(studio._directory(project.get("directory")))
    studio._directory(project.get("canonical"))
    path = PurePosixPath(directory)
    if not path.is_relative_to(root):
        _refuse()
    subpath = None if path == root else str(path.relative_to(root))
    return _LocationEvidence(info, location, project_id, subpath)


def _project_page(reply, directory, evidence, hidden):
    items, following = studio._page(reply, RECENT_LIMIT)
    projected, seen = [], set()
    for row in items:
        if (
            type(row) is not dict
            or not studio._session_id(ident := row.get("id"))
            or ident in seen
            or any(secret in ident for secret in hidden)
            or row.get("location") != {"directory": directory}
            or row.get("projectID") != evidence.project_id
            or row.get("subpath") != evidence.subpath
            or type(times := row.get("time")) is not dict
            or not all(
                studio._timestamp(times.get(key)) for key in ("created", "updated")
            )
            or ("archived" in times and not studio._timestamp(times["archived"]))
            or (
                "parentID" in row
                and (
                    not studio._session_id(row["parentID"]) or row["parentID"] == ident
                )
            )
        ):
            _refuse()
        seen.add(ident)
        title, _ = studio._plain(row.get("title"), MAX_TITLE_BYTES, hidden)
        projected.append(
            {
                "id": ident,
                "title": title.strip() or ident,
                "archived": "archived" in times,
                "child": "parentID" in row,
            }
        )
    return {
        "sessions": projected,
        # A native continuation or a full page leaves "more or unknown".
        # Native can offer a cursor even after a short final nonempty page.
        "hasEarlier": following is not None or len(items) == RECENT_LIMIT,
    }


def _native_catalog(io, lease, browser, binding, origin, deadline, *, current, check):
    owner = lease.identity
    directory = studio._directory(lease.project)
    if type(browser.native_cookie) is not web_native_pair.NativeCookie:
        _refuse()
    cookie = browser.native_cookie.upstream_cookie(
        binding=binding, endpoint=owner.endpoint, origin=origin, version=owner.version
    )
    control_value = base64.b64encode(
        ("opencode:" + lease.native_password).encode("ascii")
    ).decode("ascii")
    hidden = (
        browser._token,
        cookie.split("=", 1)[1],
        lease.native_password,
        control_value,
    )
    current()
    outcome = lease.connect_native(
        owner,
        track=web_named_relay._ConnectorRegistration(io, owner.endpoint, current),
        check=current,
        deadline=deadline,
    )
    current()
    peer = io.upstream
    if (
        outcome is not None
        or peer is None
        or type(io._workspace) is not web_named_relay._WorkspaceChild
    ):
        _refuse()
    authority = origin.value.removeprefix("https://")
    page = _list_path(directory)
    allowed = frozenset((_INFO, _LOCATION, page))

    def get(path, *, closing=False):
        if path not in allowed or (closing and path != _LOCATION):
            _refuse()
        current()
        wire = (
            f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nOrigin: {origin.value}\r\n"
            f"Cookie: {cookie}\r\nAccept: application/json\r\nAccept-Encoding: identity\r\n"
            f"Connection: {'close' if closing else 'keep-alive'}\r\n\r\n"
        ).encode("ascii")
        peer.settimeout(studio._remaining(deadline))
        peer.sendall(wire)

        def recv(size):
            check()
            peer.settimeout(studio._remaining(deadline))
            return peer.recv(size)

        return web_studio_send._read_reply(recv, closing=closing)

    before = _snapshot(get, owner, directory)
    value = _project_page(get(page), directory, before, hidden)
    if before != _snapshot(get, owner, directory, closing=True):
        _refuse()
    io.finish_workspace(deadline, current)
    if not io.cleanup_confirmed:
        _refuse()
    current()
    return value


def _cookie(app, request):
    if (
        type(request) is not web_named_transport.NamedRequest
        or type(request.role) is not str
        or request.role != "workspace"
        or type(request.method) is not str
        or request.method != "GET"
        or type(request.target) is not str
        or request.target != PATH
        or type(request.headers) is not tuple
        or type(request.body) is not bytes
        or request.body
    ):
        _refuse()
    fields = web_named_headers._fields(
        request.headers, app._origin.value.removeprefix("https://")
    )
    if (
        fields.keys() & web_named_transport._UNSUPPORTED_FIELDS
        or any(name.startswith("sec-websocket-") for name in fields)
        or "sec-fetch-user" in fields
        or "content-type" in fields
        or fields.get("sec-fetch-site") != "same-origin"
        or fields.get("sec-fetch-mode") not in ("cors", "same-origin")
        or fields.get("sec-fetch-dest") != "empty"
    ):
        _refuse()
    # The transport owner already verified TLS and its endpoint. This checks
    # the exact named origin when sent, framing, and the actual app cookie.
    return app._cookie(
        method=request.method,
        headers=request.headers,
        actual_tls=True,
        name=web_app_ingress.APP_COOKIE_NAME,
        body=request.body,
    )


class NamedStudioCatalog:
    """Borrow the original named app and the TLS worker's single exchange."""

    def __init__(self, owner):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(owner) is not web_named_owner.NamedOwnerBinding
        ):
            _refuse()
        with owner._lock:
            app, lease = owner._app, owner._lease
            if owner._closed.is_set() or type(app) is not web_named_owner.BoundNamedApp:
                _refuse()
        ingress = app.ingress
        if (
            type(ingress) is not web_app_ingress.NamedAppIngress
            or ingress._binding is not owner.binding
            or ingress._native_version != studio.NATIVE_VERSION
            or lease.identity.version != studio.NATIVE_VERSION
            or ingress._endpoint != lease.identity.endpoint
        ):
            _refuse()
        self._app, self._lease = ingress, lease

    def respond(self, stream, request, deadline, *, check):
        """Only for the verified workspace TLS worker. Any error is close-only."""
        try:
            self._respond(stream, request, deadline, check=check)
            return
        except Exception:  # Never surface raw origin, native, or transport diagnostics.
            pass
        _refuse()

    def _respond(self, stream, request, deadline, *, check):
        if not callable(check) or type(deadline) not in (int, float):
            _refuse()
        studio._remaining(deadline)
        check()
        app = self._app
        browser = app._session(_cookie(app, request))
        deadline = min(deadline, browser.expires_at, monotonic() + studio.HOST_TIMEOUT)
        io = web_named_relay._RegisteredIO(stream, owns_browser=False)
        with app._admission.connection(app._binding, browser, cancel=io.cancel) as use:
            try:

                def registered():
                    if use is None or use.cancelled:
                        _refuse()
                    check()
                    studio._remaining(deadline)
                    if use.cancelled:
                        _refuse()

                def current():
                    registered()
                    app._require_current()
                    registered()

                current()
                value = _native_catalog(
                    io,
                    self._lease,
                    browser,
                    app._binding,
                    app._origin,
                    deadline,
                    current=current,
                    check=registered,
                )
                body = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                current()
                web_named_transport._write_response(
                    stream,
                    web_named_transport.NamedResponse(200, body),
                    deadline,
                    check=current,
                )
            finally:
                cleaned = False
                try:
                    io.close()
                    cleaned = io.cleanup_confirmed is True
                finally:
                    if use is not None and not cleaned:
                        use._cleanup_unconfirmed.set()
                        app.close()
                        _refuse()
