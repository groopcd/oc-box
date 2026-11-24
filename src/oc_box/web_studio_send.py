"""Unserved, one-use native Studio text Send over an existing owner lease."""

import base64
from dataclasses import dataclass, field
from enum import Enum
import json
import math
import os
import re
import socket
import struct
from threading import Event, Lock
from time import monotonic

if __package__:
    from . import (
        web_named_owner,
        web_named_relay,
        web_native_pair,
        web_studio_conversation,
    )
else:
    import web_named_owner
    import web_named_relay
    import web_native_pair
    import web_studio_conversation

_NATIVE_VERSION = "2.4.1"
MAX_TEXT_BYTES = 4096
MAX_BODY_BYTES = 8192
_MAX_HEAD = 8192
_MAX_FIELDS = 64
_MAX_REPLY = 32 * 1024
_MAX_SECONDS = 6
_FORBIDDEN_HEADERS = {
    "transfer-encoding",
    "trailer",
    "upgrade",
    "location",
    "refresh",
    "set-cookie",
    "set-cookie2",
    "www-authenticate",
    "proxy-authenticate",
    "authentication-info",
    "proxy-authentication-info",
}


class SendError(ValueError):
    """Fixed refusal; no request, target, private credential or native diagnostic."""

    def __init__(self):
        super().__init__("The Studio Send operation could not be verified.")


class SendResult(Enum):
    REFUSED = "refused"  # No native POST attempted by this operation.
    QUEUED = "queued"  # Native acknowledgment, never evidence of execution.
    UNCONFIRMED = "unconfirmed"  # The input may already have reached OpenCode.


def _refuse():
    raise SendError() from None


@dataclass(frozen=True, slots=True, repr=False)
class SendScope:
    """Trusted shell snapshot, never construct from request fields.

    The producer attests the exact authenticated browser handle, owner run and
    selected session. The opaque browser is compared by identity, not claims.
    """

    workspace_id: str
    owner_generation: str
    container_id: str
    endpoint: str
    project: str
    version: str
    session_id: str
    selection_generation: int
    browser: object = field(compare=False)

    def __post_init__(self):
        if (
            any(
                type(value) is not str
                or len(value) != width
                or re.fullmatch(r"[a-f0-9]+", value) is None
                for value, width in (
                    (self.workspace_id, 32),
                    (self.owner_generation, 32),
                    (self.container_id, 64),
                )
            )
            or type(self.version) is not str
            or self.version != _NATIVE_VERSION
            or not web_studio_conversation._session_id(self.session_id)
            or type(self.selection_generation) is not int
            or not 0 <= self.selection_generation <= 2**53 - 1
            or self.browser is None
            or isinstance(self.browser, (str, bytes, int, float))
        ):
            _refuse()
        try:
            if web_studio_conversation._endpoint(self.endpoint)[0] != self.endpoint:
                _refuse()
            web_studio_conversation._directory(self.project)
        except ValueError:
            _refuse()


def _remaining(deadline):
    left = deadline - monotonic()
    if not math.isfinite(left) or left <= 0:
        _refuse()
    return left


def _native_body(text):
    if (
        type(text) is not str
        or not 0 < len(text) <= MAX_TEXT_BYTES
        or not text.strip()
        or any(
            (ord(char) < 32 or 127 <= ord(char) < 160) and char not in "\t\n"
            for char in text
        )
        or len(text.encode("utf-8")) > MAX_TEXT_BYTES
    ):
        _refuse()
    # Native assigns its own correctly ordered message ID. No browser ID,
    # metadata, attachments, model, agent or permission fields enter this body.
    value = {"text": text, "delivery": "queue", "resume": True}
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        _refuse()
    return body


def _acknowledged(value, session, text):
    data = web_studio_conversation._data(value)
    if (
        type(data) is not dict
        or set(data) != {"id", "sessionID", "time", "type", "payload", "delivery"}
        or not web_studio_conversation._message_id(data["id"])
        or data["sessionID"] != session
        or data["type"] != "user"
        or data["delivery"] != "queue"
        or data["payload"] != {"text": text}
        or type(times := data["time"]) is not dict
        or set(times) != {"created"}
        or not web_studio_conversation._timestamp(times["created"])
    ):
        _refuse()


def _read_reply(recv, *, closing):
    """Strict single response for the outstanding fixed request; no pipelining."""
    data = bytearray()
    separator = b"\r\n\r\n"
    while (boundary := data.find(separator)) < 0:
        room = _MAX_HEAD - len(data)
        if room <= 0 or not (chunk := recv(min(4096, room))):
            _refuse()
        data.extend(chunk)
    split = boundary + len(separator)
    head = bytes(data[:split])
    status, headers, _ = web_native_pair._reply(head)
    lengths = headers.get("content-length", ())
    if (
        not head.startswith(b"HTTP/1.1 ")
        or head[:boundary].count(b"\r\n") > _MAX_FIELDS
        or status != 200
        or headers.keys() & _FORBIDDEN_HEADERS
        or headers.get("connection") != ["close" if closing else "keep-alive"]
        or headers.get("content-encoding", []) not in ([], ["identity"])
        or headers.get("content-type") != ["application/json"]
        or len(lengths) != 1
        or re.fullmatch(r"[1-9][0-9]{0,4}", lengths[0]) is None
        or (length := int(lengths[0])) > _MAX_REPLY
    ):
        _refuse()
    body = data[split:]
    if len(body) > length:
        _refuse()
    while len(body) < length:
        chunk = recv(min(4096, length - len(body)))
        if not chunk:
            _refuse()
        body.extend(chunk)
    if closing and recv(1):
        _refuse()
    return web_studio_conversation._json(bytes(body))


def _preflight(get, scope):
    route = "/api/session/" + scope.session_id

    def target():
        evidence = web_studio_conversation._snapshot(
            get, scope.endpoint, scope.project, route, scope.session_id
        )
        session = evidence[-1]
        if "parentID" in session or "archived" in session["time"]:
            _refuse()
        return evidence

    before = target()
    web_studio_conversation._require_unmoved(get, route + "/message")
    active = web_studio_conversation._data(get("/api/session/active"))
    if (
        type(active) is not dict
        or scope.session_id in active
        or any(
            not web_studio_conversation._session_id(ident)
            or state != {"type": "running"}
            for ident, state in active.items()
        )
        or web_studio_conversation._data(get(route + "/inbox")) != []
        or target() != before
    ):
        _refuse()
    return route + "/prompt"


class StudioSend:
    """Single explicit action on a borrowed, already verified private lease.

    Register ``cancel`` with the actual browser admission before ``execute``.
    ``observe`` must synchronously recheck the same owner generation and
    exact live authorized selection, not resolve a new owner or read a stale
    browser report. It must honor the passed absolute monotonic deadline.
    The caller holds admission and the same control stream through cleanup
    and any response. Construction is not an authentication mechanism.
    """

    def __init__(self, scope, *, control, observe, deadline):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(scope) is not SendScope
            or type(control) is not socket.socket
            or control.family != socket.AF_UNIX
            or control.type != socket.SOCK_STREAM
            or not callable(observe)
            or type(deadline) not in (int, float)
        ):
            _refuse()
        try:
            _remaining(deadline)
            control.getpeername()
            _, uid, _ = struct.unpack(
                "3i", control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            self._descriptor = web_named_owner._descriptor(control)
            if uid != os.getuid() or not web_named_owner._quiet_control(
                control, self._descriptor
            ):
                _refuse()
        except (OSError, ValueError, OverflowError, struct.error):
            _refuse()
        self._scope = scope
        self._control = control
        self._observe = observe
        self._deadline = min(deadline, monotonic() + _MAX_SECONDS)
        self._lock = Lock()
        self._cancelled = Event()
        self._used = self._post_attempted = self._retired = False
        self._upstream = self._child = None
        self._cleanup_confirmed = True

    @property
    def cleanup_confirmed(self):
        """Transport completion, independent of any native prompt outcome."""
        return self._cleanup_confirmed

    def cancel(self):
        """Latch cancellation and interrupt only this operation's transport."""
        with self._lock:
            self._cancelled.set()
            if self._upstream is not None:
                try:
                    self._upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # The registered stream might not be connected yet.

    def track(self, peer):
        """Internal registration hook for the fixed managed connector."""
        with self._lock:
            if peer is self._upstream:
                _refuse()
            if self._cancelled.is_set() or self._retired or self._upstream is not None:
                peer.close()
                _refuse()
            self._upstream = peer
        return peer

    def register_workspace(self, child):
        with self._lock:
            if (
                self._cancelled.is_set()
                or self._retired
                or self._child is not None
                or self._upstream is None
            ):
                _refuse()
            self._child = child
            self._cleanup_confirmed = False

    def _time_left(self):
        if self._cancelled.is_set() or not web_named_owner._quiet_control(
            self._control, self._descriptor
        ):
            _refuse()
        return _remaining(self._deadline)

    def _current(self):
        self._time_left()
        live = self._observe(self._deadline)
        if (
            type(live) is not SendScope
            or live != self._scope
            or live.browser is not self._scope.browser
        ):
            _refuse()
        self._time_left()

    def _start(self, box):
        # Reuse the managed transport and exact exec/cleanup boundary. The
        # container port is never a host dial target and stdin carries bytes.
        web_named_relay._ConnectorRegistration(
            self, self._scope.endpoint, self._current
        ).start_workspace_relay(
            box, container_id=self._scope.container_id, endpoint=self._scope.endpoint
        )

    def _request(self, auth, path, body=None):
        closing = body is not None
        fields = [
            f"{'POST' if closing else 'GET'} {path} HTTP/1.1",
            "Host: " + self._scope.endpoint.removeprefix("http://"),
            "Authorization: Basic " + auth,
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: " + ("close" if closing else "keep-alive"),
        ]
        if closing:
            fields += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        wire = ("\r\n".join(fields) + "\r\n\r\n").encode("ascii") + (body or b"")
        peer = self._upstream
        if closing:
            self._current()
            with self._lock:
                peer.settimeout(self._time_left())
                self._post_attempted = True
        else:
            peer.settimeout(self._time_left())
        peer.sendall(wire)  # No retry, even if the kernel reports a short/failed write.

        def recv(size):
            peer.settimeout(self._time_left())
            return peer.recv(size)

        return _read_reply(recv, closing=closing)

    def _cleanup(self):
        try:
            with self._lock:
                self._retired = True  # Disallow a late or stale connector registration.
                if self._upstream is not None:
                    self._upstream.close()
                    self._upstream = None
        finally:
            if self._child is not None:
                self._child.close()
                self._cleanup_confirmed = self._child.confirmed

    def execute(self, box, *, native_password, text):
        """Attempt one explicit Send; return a fixed outcome and never retry."""
        with self._lock:
            if self._used:
                return SendResult.REFUSED
            self._used = True
        verified = False
        try:
            if (
                type(native_password) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", native_password) is None
            ):
                _refuse()
            body = _native_body(text)
            auth = base64.b64encode(
                ("opencode:" + native_password).encode("ascii")
            ).decode("ascii")
            if native_password in text or auth in text:
                _refuse()
            self._current()
            self._start(box)
            self._current()
            route = _preflight(lambda path: self._request(auth, path), self._scope)
            _acknowledged(
                self._request(auth, route, body), self._scope.session_id, text
            )
            self._child.finish(self._deadline, self._current)
            self._current()
            verified = True
        except Exception:  # noqa: BLE001 - No raw prompt or private transport details.
            pass
        finally:
            try:
                self._cleanup()
            except Exception:  # noqa: BLE001 - Do not claim that cleanup was confirmed.
                self._cleanup_confirmed = False
        if verified and self.cleanup_confirmed:
            try:
                self._current()
            except Exception:  # noqa: BLE001 - Late cancellation still withholds acknowledgment.
                pass
            else:
                return SendResult.QUEUED
        return SendResult.UNCONFIRMED if self._post_attempted else SendResult.REFUSED
