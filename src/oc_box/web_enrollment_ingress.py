"""Unserved enrollment JSON for one already host-approved account bootstrap.

No listener, cookie issuance, native admission, store access, or retry. A
trusted HTTP caller owns actual TLS/SNI, bounded raw framing and the response
write. Neither request credentials nor protocol diagnostics belong in logs.
"""

import base64
import json
import math
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass

if __package__:
    from . import (
        web_account_ingress,
        web_bootstrap,
        web_enrollment,
        web_named_headers,
        web_pake_flow,
    )
else:
    import web_account_ingress
    import web_bootstrap
    import web_enrollment
    import web_named_headers
    import web_pake_flow


START_PATH = "/enroll/start"
PROOF_START_PATH = "/enroll/proof/start"
PROOF_FINISH_PATH = "/enroll/proof/finish"
_SHAPES = {
    START_PATH: (("request", 32),),
    PROOF_START_PATH: (("challenge", None), ("registration", 192), ("ke1", 96)),
    PROOF_FINISH_PATH: (("challenge", None), ("ke3", 64)),
}
_FORBIDDEN = web_account_ingress._UNSUPPORTED_FRAMING | frozenset(
    ("proxy-connection", "http2-settings", "sec-fetch-user", "upgrade")
)


def _json(fields):
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode("ascii")


_BODY_SIZES = {
    target: len(_json({key: "" for key, _ in shape}))
    + sum(43 if width is None else 4 * ((width + 2) // 3) for _, width in shape)
    for target, shape in _SHAPES.items()
}
MAX_BODY_BYTES = max(_BODY_SIZES.values())
_MAX_TARGET = max(map(len, _SHAPES))
_MAX_REPLY_BYTES = 1024
_VERIFIED = b'{"verified":true}'


@dataclass(frozen=True, slots=True, repr=False)
class _FinalWrite:
    approval: web_enrollment.EnrollmentApproval
    http: object
    stream: socket.socket | None


@dataclass(frozen=True, slots=True, repr=False)
class EnrollmentReply:
    status: int
    body: bytes
    _write_check: Callable[["EnrollmentReply"], None] | None = None
    _abort: Callable[[], None] | None = None
    _final: _FinalWrite | None = None

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        return web_account_ingress.AccountResponse(self.status, self.body).headers

    def check_write(self):
        """The trusted writer calls this in addition to its transport gate."""
        if self._write_check is not None:
            self._write_check(self)

    def abort_write(self):
        """Fence the approval on a failed or uncertain write; never replay."""
        if type(self._final) is _FinalWrite:
            self._final.approval._abort_final_reply(self._final.http, self)
        elif self._abort is not None:
            self._abort()


_REFUSAL = EnrollmentReply(403, b'{"error":"Named enrollment refused."}')


def write_enrollment_reply(stream, reply, deadline, *, check):
    """Write one checked reply from an enrollment-only TLS worker.

    The caller owns the verified account TLS stream and closes it after this
    exchange. Do not retain callbacks or write another response on failure.
    A successful 200 for ``PROOF_FINISH_PATH`` is only verified local
    delivery; the host still authorizes and performs the store CAS separately.
    """
    if __package__:
        from . import web_named_transport
    else:
        import web_named_transport
    if type(reply) is not EnrollmentReply:
        raise web_named_headers.NamedRequestError()
    final = reply._final
    writer = object() if type(final) is _FinalWrite else None
    delivered = False
    try:
        if writer is not None:
            cutoff = final.approval._begin_final_write(final.http, reply, writer)
            if cutoff is None:
                raise web_named_headers.NamedRequestError()
        if (
            type(reply.status) is not int
            or reply.status not in (200, 403)
            or type(reply.body) is not bytes
            or not 0 < len(reply.body) <= _MAX_REPLY_BYTES
            or (reply.status == 403 and reply is not _REFUSAL)
            or (
                reply.status == 200
                and (
                    not callable(reply._write_check)
                    or (final is None and not callable(reply._abort))
                )
            )
            or (reply.body == _VERIFIED and writer is None)
            or (
                final is not None
                and (
                    writer is None
                    or reply.status != 200
                    or reply.body != _VERIFIED
                    or type(stream) not in (socket.socket, ssl.SSLSocket)
                    or stream is not final.stream
                )
            )
            or not callable(check)
            or type(deadline) not in (int, float)
            or not 0 < deadline < math.inf
        ):
            raise web_named_headers.NamedRequestError()
        if writer is not None:
            deadline = min(deadline, cutoff)

        def current():
            web_enrollment._checkpoint(check, deadline)
            reply.check_write()
            web_enrollment._checkpoint(check, deadline)

        if writer is not None:
            current()
        web_named_transport._write_response(
            stream,
            web_named_transport.NamedResponse(reply.status, reply.body),
            deadline,
            check=current,
        )
        if writer is not None:
            current()
            if not final.approval._end_final_write(
                final.http, writer, deadline=deadline
            ):
                raise web_named_headers.NamedRequestError()
        delivered = True
        return reply.status
    except Exception:
        raise web_named_headers.NamedRequestError() from None
    finally:
        if not delivered:
            try:
                if writer is not None:
                    # A copy or replay cannot cancel another writer's claim.
                    final.approval._end_final_write(final.http, writer)
                elif final is None and reply.body != _VERIFIED:
                    reply.abort_write()
            except Exception:
                # Never return a successful verdict or a second HTTP frame.
                # The approval owns the sticky teardown fence.
                raise web_named_headers.NamedRequestError() from None


def _inputs(target, body):
    shape = _SHAPES[target]
    fields = json.loads(body.decode("ascii"))
    if (
        type(fields) is not dict
        or tuple(fields) != tuple(key for key, _ in shape)
        or any(type(value) is not str for value in fields.values())
        # Reject duplicates, alternate escaping, whitespace and ordering.
        or _json(fields) != body
    ):
        raise web_named_headers.NamedRequestError()
    result = {}
    for key, width in shape:
        value = fields[key]
        if width is None:
            if not web_bootstrap._valid(value):
                raise web_named_headers.NamedRequestError()
        else:
            value = web_pake_flow._decode(value, width)
            if value is None:
                raise web_named_headers.NamedRequestError()
        result[key] = value
    return result


def _response_body(target, result):
    if target == START_PATH:
        if (
            type(result) is not web_enrollment.RegistrationResponse
            or not web_bootstrap._valid(result.challenge)
            or not web_enrollment._frame(result.response, 64)
        ):
            return None
        return _json({"challenge": result.challenge, "response": _b64(result.response)})
    if target == PROOF_START_PATH:
        if (
            type(result) is not web_enrollment.EnrollmentProofChallenge
            or not web_enrollment._frame(result.ke2, 320)
            or not web_enrollment._frame(result.context, 32)
        ):
            return None
        return _json({"ke2": _b64(result.ke2), "context": _b64(result.context)})
    return _VERIFIED if result is True else None


def _b64(value):
    return base64.b64encode(value).decode("ascii")


class EnrollmentIngress:
    """Exactly three account-site POSTs for one pre-approved coordinator."""

    def __init__(self, *, origins, approval):
        if type(
            approval
        ) is not web_enrollment.EnrollmentApproval or not approval.matches_origins(
            origins
        ):
            raise ValueError("Invalid named enrollment ingress configuration.")
        http = approval._bind_http()
        if http is None:
            raise ValueError("Invalid named enrollment ingress configuration.")
        self._account = origins.account
        self._approval = approval
        self._http = http

    def __call__(
        self,
        *,
        method,
        target,
        headers,
        body,
        actual_tls,
        check,
        deadline,
        stream=None,
    ) -> EnrollmentReply:
        """Check raw fields; no authorization is obtained from a body or URL.

        ``actual_tls`` must come from the dedicated TLS connection, never
        request/proxy data. ``check`` and the absolute monotonic ``deadline``
        are trusted transport inputs. A successful response is provisional
        until the writer checks it before/during/after its one bounded write.
        The final route binds to the original trusted stream now, not at
        write time. Transport-free fixtures can never release a host result.
        """
        if (
            type(method) is not str
            or method != "POST"
            or type(target) is not str
            or len(target) > _MAX_TARGET
            or target not in _SHAPES
            or type(headers) not in (tuple, list)
            or type(body) is not bytes
            or len(body) != _BODY_SIZES[target]
            or not callable(check)
            or type(deadline) not in (int, float)
            or not 0 < deadline < math.inf
            or (
                stream is not None
                and type(stream) not in (socket.socket, ssl.SSLSocket)
            )
        ):
            return _REFUSAL
        headers = tuple(headers[: web_named_headers._MAX_FIELDS + 1])
        try:
            nonce = web_named_headers.admit_headers(
                origin=self._account,
                headers=headers,
                method=method,
                actual_tls=actual_tls,
                cookie_name=web_account_ingress._ACCOUNT_COOKIE,
            )
            fields = {key.lower(): value.strip(" ") for key, value in headers}
            if (
                fields.get("content-type") != "application/json"
                or fields.get("content-length") != str(len(body))
                or fields.keys() & _FORBIDDEN
                or any(name.startswith("sec-websocket-") for name in fields)
                or fields.get("connection", "close").lower()
                not in ("close", "keep-alive")
                or fields.get("sec-fetch-site") != "same-origin"
                or fields.get("sec-fetch-mode") != "same-origin"
                or fields.get("sec-fetch-dest") != "empty"
            ):
                return _REFUSAL
            inputs = _inputs(target, body)
        except (ValueError, RecursionError):
            return _REFUSAL

        approval = self._approval
        http = self._http
        binding = dict(account_origin=self._account.value, account_nonce=nonce)

        def check_write(reply):
            try:
                web_enrollment._checkpoint(check, deadline)
                if not approval._reply_current(
                    **binding,
                    http=http,
                    final=reply if target == PROOF_FINISH_PATH else None,
                ):
                    raise web_named_headers.NamedRequestError()
                web_enrollment._checkpoint(None, deadline)
            except Exception:
                reply.abort_write()
                raise web_named_headers.NamedRequestError() from None

        dispatch = {
            START_PATH: approval.start,
            PROOF_START_PATH: approval.start_proof,
            PROOF_FINISH_PATH: approval.finish_proof,
        }[target]
        final = None
        if target == PROOF_FINISH_PATH:
            # Commit this exact reply identity as part of finishing KE3, not
            # by attaching writer authority to an already verified proof.
            final = EnrollmentReply(
                200, _VERIFIED, check_write, _final=_FinalWrite(approval, http, stream)
            )
            inputs["_final_reply"] = final
        try:
            # The coordinator reserves a valid matching phase before evaluating
            # the request guard, so a foreign cookie/challenge cannot cancel it.
            result = dispatch(
                **binding, **inputs, check=check, deadline=deadline, _http=http
            )
            body = _response_body(target, result)
            if body is None:
                if result is not None and result is not False:
                    approval.cancel()  # Nothing may follow an unexpected stage reply.
                return _REFUSAL
            reply = final or EnrollmentReply(200, body, check_write, approval.cancel)
            reply.check_write()
            return reply
        except Exception:  # noqa: BLE001 - the coordinator owns any sticky cleanup latch.
            if final is not None:
                final.abort_write()
            return _REFUSAL
        except BaseException:
            if final is not None:
                final.abort_write()
            raise
