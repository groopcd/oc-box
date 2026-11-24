"""Unserved, authenticated native metadata exchange for named HTTPS.

The host must already hold and verify the private owner lease, and provide a
connector for that owner's private namespace. There is deliberately no default
connector (in particular, no host-loopback dial using a container's port).
"""

import json
import math
import os
import re
import select
import signal
import socket
import subprocess
from threading import Lock
from time import monotonic, sleep

if __package__:
    from . import web_app_ingress as app
    from . import web_named_transport as https
    from . import web_native_pair as pair
    from . import workspace_service
else:
    import web_app_ingress as app
    import web_named_transport as https
    import web_native_pair as pair
    import workspace_service


_HEAD_LIMIT = 8192
_LENGTH = re.compile(r"[1-9][0-9]{0,3}")
_SESSION_ID = re.compile(r"ses[A-Za-z0-9_-]{1,128}")
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}")
_WAIT_SLICE = 0.05
_EOF_GRACE = 1.0
_REAP_GRACE = 0.5
_UNSUPPORTED = frozenset(
    (
        "transfer-encoding",
        "trailer",
        "upgrade",
        "location",
        "refresh",
        "www-authenticate",
        "proxy-authenticate",
        "authentication-info",
        "proxy-authentication-info",
    )
)


def _refuse():
    raise app.AppRequestError()


class _WorkspaceChild:
    """Exactly one fixed exec; only its request worker may signal or reap it."""

    def __init__(self):
        self._attempted = False
        self._child = self._pidfd = self._exit = None
        self._forced = False

    def start(self, command, stdio):
        if (
            not hasattr(os, "pidfd_open")
            or not hasattr(signal, "pidfd_send_signal")
            or signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN
        ):
            _refuse()
        # Latch before entering Popen. A startup exception without a returned
        # handle is not proof nothing started: even Popen's own descriptor
        # handling can fail after fork.
        self._attempted = True
        self._child = subprocess.Popen(
            command,
            env=command.environment,
            stdin=stdio,
            stdout=stdio,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            cwd="/",
        )
        self._pidfd = os.pidfd_open(self._child.pid)

    @property
    def confirmed(self):
        return not self._attempted or (self._exit == 0 and not self._forced)

    def _wait(self, deadline, check=None):
        child = self._child
        poller = None
        if self._pidfd is not None and self._exit is None:
            poller = select.poll()
            poller.register(self._pidfd, select.POLLIN)
        while self._exit is None:
            if check is not None:
                check()
            # Popen.wait/poll can synthesize a zero for ECHILD. Only a
            # terminal status from waitpid on our exact child is evidence.
            pid, status = os.waitpid(child.pid, os.WNOHANG)
            if pid == child.pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
                self._exit = child.returncode = os.waitstatus_to_exitcode(status)
                break
            delay = min(_WAIT_SLICE, deadline - monotonic())
            if delay <= 0:
                break
            if poller is None:
                sleep(delay)
            else:
                poller.poll(math.ceil(1000 * delay))
        return self._exit

    def finish(self, deadline, check):
        if self._child is None or self._wait(deadline, check) != 0:
            _refuse()
        check()

    def close(self):
        # By now both copies of our socketpair are closed by the host. Give
        # the exec path a bounded chance to propagate stdin EOF and exit on
        # its own. Killing only the host Podman process does not prove that
        # its in-container relay exited.
        if self._child is None:
            return
        try:
            if self._wait(monotonic() + _EOF_GRACE) is None and self._pidfd is not None:
                try:
                    signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The exact child may have exited at the grace boundary.
                else:
                    self._forced = True
                self._wait(monotonic() + _REAP_GRACE)
        finally:
            if self._exit is not None and self._pidfd is not None:
                os.close(self._pidfd)
                self._pidfd = None


class _RegisteredIO:
    """Own this request's sockets and optional exec from startup through drain."""

    def __init__(self, browser, *, owns_browser):
        self.browser = browser
        self._owns_browser = owns_browser
        self.upstream = None
        self._workspace = None
        self._lock = Lock()
        self._cancelled = False

    def track(self, upstream):
        if type(upstream) is not socket.socket:
            _refuse()
        with self._lock:
            if upstream is self.upstream:
                _refuse()
            if (
                self._cancelled
                or self.upstream is not None
                or (upstream.type != socket.SOCK_STREAM)
            ):
                upstream.close()
                _refuse()
            self.upstream = upstream
        return upstream

    def register_workspace(self, child):
        with self._lock:
            if self._cancelled or self._workspace is not None or self.upstream is None:
                _refuse()
            self._workspace = child

    def finish_workspace(self, deadline, check):
        if self._workspace is not None:
            self._workspace.finish(deadline, check)

    @property
    def cleanup_confirmed(self):
        return self._workspace is None or self._workspace.confirmed

    def cancel(self):
        # Revocation must not wait on I/O or close a borrowed/reused socket.
        # Worker cleanup owns close; shutdown wakes a running read or write.
        with self._lock:
            self._cancelled = True
            for peer in (self.browser, self.upstream):
                if peer is not None:
                    try:
                        # SSLSocket.shutdown clears its TLS object. Leave TLS
                        # state to the worker; interrupt only the underlying TCP.
                        socket.socket.shutdown(peer, socket.SHUT_RDWR)
                    except OSError:
                        pass  # May not yet be connected, or may already be closed.

    def close(self):
        try:
            with self._lock:
                self._cancelled = True  # Never accept a stale registration hook.
                try:
                    if self.upstream is not None:
                        self.upstream.close()
                finally:
                    if self._owns_browser:
                        self.browser.close()
                    else:
                        # The site worker alone checks identity and closes its
                        # TLS stream. Late cancellation must not touch it.
                        self.browser = None
        finally:
            if self._workspace is not None:
                self._workspace.close()


class _ConnectorRegistration:
    """Keep the original direct-socket hook; add only the fixed exec transport."""

    def __init__(self, io, endpoint, check):
        self._io, self._endpoint, self._check = io, endpoint, check

    def __call__(self, peer):
        peer = self._io.track(peer)
        self._check()
        return peer

    def start_workspace_relay(self, box, *, container_id, endpoint):
        if (
            os.environ.get("OC_BOX_CONTAINER") == "1"
            or type(container_id) is not str
            or len(container_id) != 64
            or _CONTAINER_ID.fullmatch(container_id) is None
            or endpoint != self._endpoint
        ):
            _refuse()
        self._check()
        port = pair._port(endpoint)
        command = workspace_service.relay_command(
            box, {"container": container_id}, port
        )
        if command != [
            "podman",
            "exec",
            "-i",
            container_id,
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "python3",
            "-c",
            workspace_service.PROXY,
            str(port),
        ]:
            _refuse()
        # Use the same worker but do not load modules from the project
        # (Podman's configured workdir) or Python site hooks on this path.
        command[3:3] = ["--workdir", "/"]
        command[-3:-3] = ["-I", "-S", "-B"]
        child = _WorkspaceChild()
        peer, stdio = socket.socketpair()
        with stdio:
            self(peer)
            self._io.register_workspace(child)
            self._check()
            child.start(command, stdio)
        self._check()


def _recv(peer, count, deadline, check):
    check()
    peer.settimeout(https._remaining(deadline))
    return peer.recv(count)


def _read_reply(peer, *, deadline, check):
    """Require one final, complete, fixed-length response and EOF."""
    pending = bytearray()
    boundary = https._HEAD_END
    scan = 0
    while (end := pending.find(boundary, scan)) < 0:
        if len(pending) == _HEAD_LIMIT:
            _refuse()
        scan = max(0, len(pending) - len(boundary) + 1)
        chunk = _recv(peer, min(4096, _HEAD_LIMIT - len(pending)), deadline, check)
        if not chunk:
            _refuse()
        pending.extend(chunk)
    if pending.count(b"\r\n", 0, end) > https.web_named_headers._MAX_FIELDS:
        _refuse()
    split = end + len(boundary)
    try:
        status, headers, _ = pair._reply(bytes(pending[:split]))
    except pair.NativePairError:
        _refuse()
    length = headers.get("content-length", ())
    if (
        status != 200
        or headers.keys() & _UNSUPPORTED
        or headers.get("content-type") != ["application/json"]
        or headers.get("content-encoding", []) not in ([], ["identity"])
        or headers.get("connection", []) not in ([], ["close"])
        or len(length) != 1
        or _LENGTH.fullmatch(length[0]) is None
        or (size := int(length[0])) > https._RESPONSE_LIMIT
    ):
        _refuse()
    body = pending[split:]
    if len(body) > size:
        _refuse()
    while len(body) < size:
        chunk = _recv(peer, min(4096, size - len(body)), deadline, check)
        if not chunk:
            _refuse()
        body.extend(chunk)
    if _recv(peer, 1, deadline, check):
        _refuse()
    return bytes(body)


def _metadata(body, target, endpoint, version):
    # Native info carries private hostname/path/capability data. Validate the
    # exact identity, then render only the already-allowed public metadata.
    try:
        value = pair.named_native_json(body)
    except ValueError:
        _refuse()
    if type(value) is not dict:
        _refuse()
    if target == "/api/info":
        try:
            public = pair.named_native_info(value, version, endpoint)
        except pair.NamedNativeWireError:
            _refuse()
        return json.dumps(public, separators=(",", ":")).encode("ascii")
    elif target == "/api/session/active":
        if (
            value.keys() != {"data"}
            or type(value["data"]) is not dict
            or any(
                _SESSION_ID.fullmatch(ident) is None or state != {"type": "running"}
                for ident, state in value["data"].items()
            )
        ):
            _refuse()
        return body
    else:
        _refuse()


class NamedNativeRelay:
    """Trusted composition with the app's *same* admission and lease.

    ``connect_native(endpoint, *, track, check, deadline)`` must synchronously
    establish one plain stream to that verified endpoint in its own namespace.
    A direct connector gives its new socket to ``track`` before connecting and
    leaves no independent work. The managed connector instead calls
    ``track.start_workspace_relay`` with the verified full container ID and
    exact endpoint; only that method starts, transfers and tracks an exec.
    Either connector must return None, send no request bytes, check admission
    during startup and observe the monotonic deadline. The relay owns tracked
    resources even if startup fails; it cannot preempt arbitrary connector code.
    """

    def __init__(self, ingress, *, connect_native):
        if type(ingress) is not app.NamedAppIngress or not callable(connect_native):
            raise ValueError("Invalid named native relay composition.")
        self._ingress = ingress
        self._connect_native = connect_native

    def respond_one(self, reader, peer):
        """Take one accepted workspace TLS connection; never bind or accept."""
        if type(reader) is not https.NamedHTTPSReader:
            with peer:
                _refuse()
        reader._exchange(peer, handlers=None, native=self._respond)

    def wait_drained(self, timeout=5.0):
        """After ingress close, require drain before releasing the owner lease."""
        return self._ingress._admission.wait_drained(timeout)

    def respond_site(self, stream, request, deadline, *, check):
        """Borrow an already admitted site's TLS stream for one exchange."""
        if not callable(check):
            _refuse()
        self._respond(stream, request, deadline, site_check=check)

    def _respond(self, stream, request, deadline, *, site_check=None):
        if (
            type(request) is not https.NamedRequest
            or request.role != "workspace"
            or request.method != "GET"
            or request.target not in app._METADATA_PATHS
            or request.body != b""
        ):
            _refuse()
        ingress = self._ingress
        token = ingress._cookie(
            method=request.method,
            headers=request.headers,
            actual_tls=True,  # Only entered by the TLS reader after parsing.
            name=app.APP_COOKIE_NAME,
            body=request.body,
        )
        # Unlike fixed status, same-origin Fetch Metadata alone is not enough.
        if not any(name.lower() == "origin" for name, _ in request.headers):
            _refuse()
        session = ingress._session(token)
        deadline = min(deadline, session.expires_at)
        io = _RegisteredIO(stream, owns_browser=site_check is None)
        with ingress._admission.connection(
            ingress._binding, session, cancel=io.cancel
        ) as use:
            try:

                def check():
                    if site_check is not None:
                        site_check()
                    if use is None or use.cancelled:
                        _refuse()
                    https._remaining(deadline)

                def current():
                    check()
                    ingress._require_current()
                    check()

                current()
                if type(session.native_cookie) is not pair.NativeCookie:
                    _refuse()
                try:
                    cookie = session.native_cookie.upstream_cookie(
                        binding=ingress._binding,
                        endpoint=ingress._endpoint,
                        origin=ingress._origin,
                        version=ingress._native_version,
                    )
                except pair.NativePairError:
                    ingress.close()  # Never rebind or repair by pairing again.
                    _refuse()
                current()
                outcome = self._connect_native(
                    ingress._endpoint,
                    track=_ConnectorRegistration(io, ingress._endpoint, current),
                    check=current,
                    deadline=deadline,
                )
                current()
                if outcome is not None or io.upstream is None:
                    _refuse()
                self._exchange(io, request.target, deadline, cookie, check, current)
            finally:
                try:
                    io.close()
                finally:
                    if not io.cleanup_confirmed:
                        # Do not advertise stdout EOF, host-only kill, or an
                        # unconfirmed reap as successful drain. Keep the use
                        # charged even if its browser previously logged out.
                        use._cleanup_unconfirmed.set()
                        ingress.close()
                        _refuse()

    def _exchange(self, io, target, deadline, cookie, check, current):
        ingress = self._ingress
        origin = ingress._origin.value
        fields = (
            f"GET {target} HTTP/1.1",
            f"Host: {origin.removeprefix('https://')}",
            f"Origin: {origin}",
            f"Cookie: {cookie}",
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: close",
        )
        peer = io.upstream
        wire = ("\r\n".join(fields) + "\r\n\r\n").encode("ascii")
        current()
        peer.settimeout(https._remaining(deadline))
        peer.sendall(wire)
        body = _read_reply(peer, deadline=deadline, check=check)
        body = _metadata(body, target, ingress._endpoint, ingress._native_version)
        io.finish_workspace(deadline, current)
        # Emit host-owned headers only. Never copy native cookies (including
        # rotations), headers, reason phrases or response status to the client.
        https._write_response(
            io.browser, https.NamedResponse(200, body), deadline, check=current
        )
