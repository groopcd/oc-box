"""First-party dispatch for one accepted named HTTPS request.

The exact prelogin, account protocol, confirm, logout and read-only native
metadata routes share the same owner/account lifetime. No binding, accepting,
generic native forwarding, Studio assets or public command are enabled here.
"""

import socket

if __package__:
    from . import (
        web_account_ingress,
        web_app_ingress,
        web_named_bootstrap,
        web_named_handoff,
        web_named_headers,
        web_named_services,
        web_named_transport,
    )
else:
    import web_account_ingress
    import web_app_ingress
    import web_named_bootstrap
    import web_named_handoff
    import web_named_headers
    import web_named_services
    import web_named_transport


_APP_ERROR = web_named_transport.NamedResponse(
    403, b'{"error":"Named request refused."}'
)
_DRAIN_ERROR = web_named_transport.NamedResponse(
    503, b'{"error":"Named request could not be completed."}'
)
_OK = web_named_transport.NamedResponse(200, b'{"ok":true}')


def _arguments(request):
    return dict(
        method=request.method,
        target=request.target,
        headers=request.headers,
        body=request.body,
        actual_tls=True,
    )


def _interrupt(stream):
    try:
        # Wake I/O without mutating the SSLSocket's TLS state. Its worker
        # still owns the object and closes it.
        socket.socket.shutdown(stream, socket.SHUT_RDWR)
    except OSError:
        pass


class NamedHTTPApplication:
    def __init__(self, *, reader, services, login_document):
        if (
            type(reader) is not web_named_transport.NamedHTTPSReader
            or type(services) is not web_named_services.NamedServices
            or services.entry._origins != reader._origins
            or type(login_document) is not bytes
            or not 0 < len(login_document) <= web_named_transport._DOCUMENT_LIMIT
        ):
            raise ValueError("Invalid named HTTPS application configuration.")
        self._reader = reader
        self._services = services
        self._prelogin = reader._prelogin_handler(services.entry, login_document)
        self._handoff = web_named_handoff.NamedHandoffIngress(
            origins=reader._origins,
            app=services.application.ingress,
        )

    def respond_one(self, peer):
        """Take one already accepted socket; the reader determines its role."""
        self._reader._exchange(peer, handlers=None, named=self._dispatch)

    def _dispatch(self, stream, request, deadline):
        if type(request) is not web_named_transport.NamedRequest:
            raise web_named_headers.NamedRequestError()
        write = web_named_transport._write_response
        app = self._services.application.ingress
        if (request.role, request.target) in (
            ("account", web_named_bootstrap.ACCOUNT_ENTRY),
            ("workspace", web_named_bootstrap.WORKSPACE_ENTRY),
        ):
            self._prelogin(stream, request, deadline)
        elif request.role == "account" and request.target in (
            web_account_ingress.START_PATH,
            web_account_ingress.FINISH_PATH,
        ):
            result = self._services.account(**_arguments(request))
            reply = web_named_transport.NamedResponse(result.status, result.body)
            write(
                stream,
                reply,
                deadline,
                check=app._require_current if reply.status == 200 else None,
            )
        elif (
            request.role == "workspace"
            and request.target in web_app_ingress._METADATA_PATHS
        ):
            self._services.application.relay._respond(stream, request, deadline)
        elif (
            request.role == "workspace"
            and request.target == web_named_handoff.HANDOFF_PATH
        ):
            try:
                reply = self._handoff.receive(**_arguments(request))
            except web_named_headers.NamedRequestError:
                write(stream, _APP_ERROR, deadline)
            else:
                self._deliver_handoff(stream, app, reply, deadline)
        elif (
            request.role == "workspace"
            and request.target == web_app_ingress.CONFIRM_PATH
        ):
            try:
                token = self._handoff.confirm_admission(**_arguments(request))
            except web_named_headers.NamedRequestError:
                write(stream, _APP_ERROR, deadline)
            else:
                self._deliver_cookie(stream, app, token, deadline)
        elif (
            request.role == "workspace"
            and request.target == web_app_ingress.LOGOUT_PATH
        ):
            try:
                drained = app.logout(
                    **_arguments(request),
                    timeout=web_named_transport._remaining(deadline),
                )
            except web_app_ingress.AppRequestError:
                write(stream, _APP_ERROR, deadline)
            else:
                if drained is True:
                    write(
                        stream,
                        _OK,
                        deadline,
                        clear_app=True,
                        check=app._require_current,
                    )
                else:
                    write(stream, _DRAIN_ERROR, deadline)
        else:
            raise web_named_headers.NamedRequestError()

    @staticmethod
    def _deliver_handoff(stream, app, reply, deadline):
        if (
            type(reply) is not web_named_handoff.HandoffReply
            or reply.status != 200
            or type(reply.body) is not bytes
            or not 0 < len(reply.body) <= web_named_transport._DOCUMENT_LIMIT
            or reply.headers[:-2] != web_named_handoff._FIXED_HEADERS
            or reply.headers[-2] != ("Content-Type", "text/html; charset=utf-8")
            or reply.headers[-1][0] != "Content-Security-Policy"
            or any(
                not 0 < len(value) <= 1024
                or any(not 32 <= ord(ch) < 127 for ch in value)
                for _, value in reply.headers
            )
        ):
            raise web_named_headers.NamedRequestError()
        headers = "".join(f"{key}: {value}\r\n" for key, value in reply.headers)
        wire = (
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
            + headers.encode("ascii")
            + f"Content-Length: {len(reply.body)}\r\n\r\n".encode("ascii")
            + reply.body
        )
        web_named_transport._write_wire(
            stream, wire, deadline, check=app._require_current
        )

    @staticmethod
    def _deliver_cookie(stream, app, token, deadline):
        # Register delivery under the newly minted browser admission. A loss
        # of this exact grant, the owner, or the write deadline never leaves
        # an undisclosed usable grant or publishes a native cookie.
        session = app._session(token)
        delivered = False
        try:
            with app._admission.connection(
                app._binding, session, cancel=lambda: _interrupt(stream)
            ) as use:

                def current():
                    if use is None or use.cancelled:
                        raise web_app_ingress.StaleAppSessionError()
                    app._require_current()
                    if use.cancelled:
                        raise web_app_ingress.StaleAppSessionError()

                web_named_transport._write_app_grant(
                    stream,
                    token,
                    min(deadline, session.expires_at),
                    check=current,
                )
                delivered = True
        finally:
            if not delivered:
                app._admission.begin_revocation(app._binding, session)
