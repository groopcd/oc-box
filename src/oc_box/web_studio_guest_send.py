"""One bounded cookie-authenticated Send over the original guest relay."""

from time import monotonic

if __package__:
    from . import (
        web_guest_duplex_exec,
        web_guest_duplex_worker,
        web_named_native_profile,
        web_studio_guest,
        web_studio_guest_binding,
        web_studio_guest_selection,
        web_studio_send,
    )
else:
    import web_guest_duplex_exec
    import web_guest_duplex_worker
    import web_named_native_profile
    import web_studio_guest
    import web_studio_guest_binding
    import web_studio_guest_selection
    import web_studio_send


def _refuse():
    raise web_studio_send.SendError()


class _Peer:
    """Bounded HTTP exchange on one already owned OGD pipe pair."""

    def __init__(
        self, tunnel, binding, cookie, deadline, *, check, current, identity=None
    ):
        if web_studio_guest._native_headers(binding, cookie) is None:
            _refuse()
        self.tunnel = tunnel
        self._binding = binding
        self._cookie = cookie
        self._deadline = deadline
        self._check = check
        self._current = current
        self._identity = identity
        self._pending = None
        self.post_attempted = False

    def _step(self):
        self._check()
        left = web_studio_send._remaining(self._deadline)
        self.tunnel.advance(milliseconds=min(25, int(left * 1000)))

    def open(self):
        self._current()
        self.tunnel.open()
        while not self.tunnel.ready:
            self._step()

    def _recv(self, size):
        while not self._pending:
            self._check()
            block = self.tunnel.receive()
            if block is not None:
                self._pending = memoryview(block)
                break
            if self.tunnel.ended:
                return b""
            self._step()
        result = bytes(self._pending[:size])
        self._pending = self._pending[len(result) :]
        if not self._pending:
            self._pending = None
            self.tunnel.consumed()
        return result

    def query(
        self,
        path,
        *,
        body=None,
        closing=False,
        limit=web_studio_send._MAX_REPLY,
        raw=False,
    ):
        if self._pending or self.post_attempted:
            _refuse()
        self._current()
        post = body is not None
        closing = closing or post
        fields = [
            f"{'POST' if post else 'GET'} {path} HTTP/1.1",
            "Host: " + self._binding.authority,
            "Origin: " + self._binding.origin,
            "Cookie: " + self._cookie,
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: " + ("close" if closing else "keep-alive"),
        ]
        if post:
            fields += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        wire = ("\r\n".join(fields) + "\r\n\r\n").encode("ascii") + (body or b"")
        if len(wire) > web_guest_duplex_worker.MAX_DATA:
            _refuse()
        while self.tunnel._queued_data is not None:
            if self.tunnel.ended:
                _refuse()
            self._step()
        self._current()
        if self.tunnel.ended or not self.tunnel.send(wire):
            _refuse()
        if post:
            # The next poll can put bytes on the native socket. From here,
            # uncertainty is not grounds for retry or replay.
            self.post_attempted = True
        response = web_studio_send._read_reply(
            self._recv, closing=closing, limit=limit, raw=raw
        )
        if self._pending:
            _refuse()
        self._current()
        if path == "/api/info" and self._identity is not None:
            ident = self._identity
            info = web_named_native_profile.named_native_info(
                web_named_native_profile.named_native_json(response)
                if raw
                else response,
                ident.version,
                ident.endpoint,
            )
            if info["pid"] != ident.native_pid:
                _refuse()
        return response


def send(
    binding,
    workers,
    worker_use,
    admission,
    use,
    grant,
    selections,
    *,
    session_id,
    generation,
    text,
    cookie,
):
    """Internal, unserved Send seam; return (fixed outcome, exact relay proof).

    The route owner must have validated its exact request's origin and schema
    and registered cancellation for the provided use. This function consumes
    native authentication on that use with the *same* request's Cookie; there
    is no privileged native password, Basic auth or named browser fallback.
    The second result concerns the new tunnel only. The caller still accounts
    for the cookie-verification helper and the browser exchange independently.
    """
    refused = web_studio_send.SendResult.REFUSED
    unconfirmed = web_studio_send.SendResult.UNCONFIRMED
    if (
        type(binding) is not web_studio_guest_binding._UnservedBinding
        or type(admission) is not web_studio_guest.GuestAdmission
        or type(selections) is not web_studio_guest_selection.GuestSelections
        or admission._binding is not binding.native.binding
        or selections._admission is not admission
        or type(use) is not web_studio_guest.GuestUse
        or type(grant) is not web_studio_guest.GuestGrant
    ):
        return refused, True
    relay = peer = None
    verified = False
    retired = True
    try:
        body = web_studio_send._native_body(text)
        if not admission.authorize_native(use, grant, cookie=cookie):
            return refused, True
        view = selections.snapshot(use, session_id=session_id, generation=generation)
        deadline = min(use.deadline, monotonic() + 6.0)
        ident = binding.identity
        owner = binding.owner
        scope = web_studio_send.SendScope(
            ident.workspace_id,
            ident.generation,
            ident.container_id,
            ident.endpoint,
            owner.owner.box.container_project(owner.data),
            ident.version,
            view.session_id,
            view.generation,
            grant,
        )

        def check():
            if (
                not workers.current(worker_use)
                or use.cancelled
                or monotonic() >= deadline
            ):
                _refuse()

        def current():
            check()
            if selections.observe(use, view) is not view:
                _refuse()
            check()

        current()
        relay = web_guest_duplex_exec.GuestDuplexExec(binding, workers, worker_use)
        tunnel = relay.start(deadline=deadline)
        peer = _Peer(
            tunnel,
            binding.native.binding,
            cookie,
            deadline,
            check=check,
            current=current,
            identity=ident,
        )
        peer.open()
        route = web_studio_send._preflight(peer.query, scope)
        web_studio_send._acknowledged(
            peer.query(route, body=body), view.session_id, text
        )
        current()
        verified = True
    except Exception:
        # Never reflect private request bytes or transport failures.
        pass
    finally:
        if relay is not None:
            try:
                retired = relay.retire() is True
            except Exception:
                retired = False
    if verified and retired:
        try:
            current()
        except Exception:
            pass
        else:
            return web_studio_send.SendResult.QUEUED, True
    return (
        unconfirmed if peer is not None and peer.post_attempted else refused
    ), retired
