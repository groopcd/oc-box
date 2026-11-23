"""Internal JSON dispatch for the dedicated loopback HTTPS account site.

This does not listen, select a workspace, mint a bootstrap, or redeem a ticket.
"""

import base64
import json
from dataclasses import dataclass
from secrets import compare_digest

if __package__:
    from . import (
        web_accounts,
        web_handoff,
        web_local_origins,
        web_named_headers,
        web_named_local,
        web_pake_flow,
    )
else:
    import web_accounts
    import web_handoff
    import web_local_origins
    import web_named_headers
    import web_named_local
    import web_pake_flow

START_PATH = "/start"
FINISH_PATH = "/finish"
MAX_BODY_BYTES = 8192
_ACCOUNT_COOKIE = "__Host-ocbox-account"
_MAX_ENCODED_MESSAGE = ((web_pake_flow.MAX_MESSAGE_BYTES + 2) // 3) * 4
_UNSUPPORTED_FRAMING = frozenset(
    ("transfer-encoding", "content-encoding", "trailer", "te", "expect")
)
_RESPONSE_HEADERS = (
    ("Content-Type", "application/json"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    (
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    ),
)


@dataclass(frozen=True, slots=True, repr=False)
class AccountResponse:
    status: int
    body: bytes

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        return _RESPONSE_HEADERS + (("Content-Length", str(len(self.body))),)


_REFUSAL = AccountResponse(403, b'{"error":"Named sign-in refused."}')


def _object(pairs):
    if len(pairs) != 2 or pairs[0][0] == pairs[1][0]:
        raise ValueError
    return dict(pairs)


def _reject_number(_):
    raise ValueError


def _protocol(value):
    if type(value) is not str or not 4 <= len(value) <= _MAX_ENCODED_MESSAGE:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return False
    return (
        0 < len(raw) <= web_pake_flow.MAX_MESSAGE_BYTES
        and base64.b64encode(raw).decode("ascii") == value
    )


def _inputs(target, body):
    fields = json.loads(
        body.decode("ascii"),
        object_pairs_hook=_object,
        parse_int=_reject_number,
        parse_float=_reject_number,
        parse_constant=_reject_number,
    )
    if type(fields) is not dict:
        raise ValueError
    if target == START_PATH:
        if fields.keys() != {"username", "ke1"} or not _protocol(fields["ke1"]):
            raise ValueError
        web_accounts._username(fields["username"])
    elif (
        fields.keys() != {"challenge_id", "ke3"}
        or not web_pake_flow._token(fields["challenge_id"])
        or not _protocol(fields["ke3"])
    ):
        raise ValueError
    # Exact suite lengths and proof verification remain PakeFlow's responsibility.
    return fields


def _reply(target, result):
    if target == START_PATH:
        if (
            type(result) is not web_pake_flow.PakeChallenge
            or not web_pake_flow._token(result.challenge_id)
            or not _protocol(result.ke2)
            or web_pake_flow._decode(result.context, 32) is None
        ):
            return _REFUSAL
        payload = {
            "challenge_id": result.challenge_id,
            "ke2": result.ke2,
            "context": result.context,
        }
    else:
        if type(result) is not web_handoff.HandoffToken or not web_handoff._valid(
            result.value, 54, web_handoff._TOKEN
        ):
            return _REFUSAL
        payload = {"ticket": result.value}
    return AccountResponse(
        200, json.dumps(payload, separators=(",", ":")).encode("ascii")
    )


class AccountIngress:
    """Dispatch exact account POSTs; all request refusals share one response.

    The trusted host caller supplies a matching local HTTPS profile and sign-in
    for a verified owner. ``translate_nonce(account_cookie)`` must look up a live,
    paired bootstrap for that same owner and return the distinct app-side nonce
    (or None). It must not mint a pair, pass the account cookie through, or merely
    check its syntax. Keep request metadata, body data, tokens and exceptions
    out of logs.
    """

    def __init__(self, *, origins, sign_in, translate_nonce):
        if (
            type(origins) is not web_local_origins.LocalOrigins
            or type(sign_in) is not web_named_local.LocalNamedSignIn
            or not callable(translate_nonce)
            or sign_in._auth_origin != origins.account.value
            or sign_in._workspace_origin != origins.workspace
        ):
            raise ValueError("Invalid named account ingress configuration.")
        self._account = origins.account
        self._sign_in = sign_in
        self._translate_nonce = translate_nonce

    def __call__(self, *, method, target, headers, body, actual_tls) -> AccountResponse:
        """Handle the raw target, complete headers, and one bounded body.

        The caller must require the account listener's loopback bind and exact TLS
        SNI. ``actual_tls`` comes from that connection, never a header. Preserve the
        raw target and duplicate header fields. Bound reads, deadlines and concurrency
        before dispatch. Pass the original body bytes (at most ``MAX_BODY_BYTES``),
        not a decoded stream; never reuse a connection with uncertain framing.

        A successful finish returns a ticket for the separate app handoff POST
        body, never a navigation URL or cookie.
        """
        if (
            type(method) is not str
            or method != "POST"
            or type(target) is not str
            or target not in (START_PATH, FINISH_PATH)
            or type(headers) not in (list, tuple)
            or type(body) is not bytes
            or not 0 < len(body) <= MAX_BODY_BYTES
        ):
            return _REFUSAL
        # Snapshot one field past the shared limit so the gate rejects overflow.
        headers = tuple(headers[: web_named_headers._MAX_FIELDS + 1])
        try:
            account_cookie = web_named_headers.admit_headers(
                origin=self._account,
                headers=headers,
                method=method,
                actual_tls=actual_tls,
                cookie_name=_ACCOUNT_COOKIE,
            )
            fields = {key.lower(): value.strip(" ") for key, value in headers}
            if (
                fields.get("content-type") != "application/json"
                or fields.get("content-length") != str(len(body))
                or fields.keys() & _UNSUPPORTED_FRAMING
                or fields.get("connection", "close").lower()
                not in ("close", "keep-alive")
                or fields.get("sec-fetch-site", "same-origin") != "same-origin"
            ):
                return _REFUSAL
            inputs = _inputs(target, body)
        except (ValueError, RecursionError):
            return _REFUSAL
        try:
            nonce = self._translate_nonce(account_cookie)
            if not web_pake_flow._token(nonce) or compare_digest(nonce, account_cookie):
                return _REFUSAL
            dispatch = (
                self._sign_in.start if target == START_PATH else self._sign_in.finish
            )
            return _reply(target, dispatch(browser_nonce=nonce, **inputs))
        except Exception:  # noqa: BLE001 - do not expose bootstrap or driver errors.
            return _REFUSAL
