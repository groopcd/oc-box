"""Narrow, unserved pre-login transition between the two named HTTPS sites.

The TLS listener must frame one bounded request, select the role from its
bound socket and exact SNI, and recheck the owner before releasing the reply.
No native routes, credentials, or workspace grants are served here.
"""

import re
from dataclasses import dataclass
from html import escape

if __package__:
    from . import web_admission, web_bootstrap, web_local_origins, web_named_headers
else:
    import web_admission
    import web_bootstrap
    import web_local_origins
    import web_named_headers


WORKSPACE_ENTRY = "/auth/oc-box-entry"
ACCOUNT_ENTRY = "/auth/oc-box-account-entry"
_APP_BOOTSTRAP = "__Host-ocbox-bootstrap"
_ACCOUNT_BOOTSTRAP = "__Host-ocbox-account"
_TICKET = re.compile(rb"ticket=([A-Za-z0-9_-]{42}[AEIMQUYcgkosw048])")
_UNSUPPORTED = frozenset(("transfer-encoding", "expect", "trailer", "upgrade"))


def _refuse():
    raise web_named_headers.NamedRequestError()


def _cookie(name, nonce):
    if not web_bootstrap._valid(nonce):
        _refuse()
    return f"{name}={nonce}; Secure; HttpOnly; SameSite=Strict; Path=/"


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceEntry:
    html: bytes
    cookie: str
    policy: str

    def headers(self):
        return (
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "origin"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", self.policy),
            ("Set-Cookie", self.cookie),
        )


@dataclass(frozen=True, slots=True, repr=False)
class AccountEntry:
    """Only the account bootstrap cookie, not authentication or a login page."""

    cookie: str


class NamedBootstrapIngress:
    def __init__(self, *, origins, binding, bootstrap, owner_current):
        if (
            type(origins) is not web_local_origins.LocalOrigins
            or type(binding) is not web_admission.WebBinding
            or binding.origin != origins.workspace.value
            or type(bootstrap) is not web_bootstrap.BrowserBootstrap
            or not bootstrap._matches(binding, origins.account.value)
            or not callable(owner_current)
        ):
            raise ValueError("Invalid named bootstrap composition.")
        self._origins = origins
        self._binding = binding
        self._bootstrap = bootstrap
        self._owner_current = owner_current

    def _live(self):
        try:
            if self._owner_current(self._binding) is True:
                return
        except Exception:  # noqa: BLE001 - never disclose owner discovery diagnostics.
            pass
        self._bootstrap.close()
        _refuse()

    @staticmethod
    def _document_fields(*, origin, headers, method, actual_tls, form_origin=None):
        web_named_headers.admit_headers(
            origin=origin,
            headers=headers,
            method=method,
            actual_tls=actual_tls,
            form_origin=form_origin,
        )
        fields = web_named_headers._fields(
            headers, origin.value.removeprefix("https://")
        )
        if (
            fields.keys() & _UNSUPPORTED
            or fields.get("connection", "close").lower() not in ("close", "keep-alive")
            or fields.get("sec-fetch-mode") != "navigate"
            or fields.get("sec-fetch-dest") != "document"
        ):
            _refuse()
        return fields

    def workspace(self, *, method, target, headers, body, actual_tls):
        if (
            method != "GET"
            or target != WORKSPACE_ENTRY
            or type(body) is not bytes
            or body
        ):
            _refuse()
        fields = self._document_fields(
            origin=self._origins.workspace,
            headers=headers,
            method=method,
            actual_tls=actual_tls,
        )
        if fields.get("content-length", "0") != "0":
            _refuse()
        self._live()
        issued = self._bootstrap.issue(self._binding)
        self._live()
        if issued is None or not web_bootstrap._valid(issued.value):
            _refuse()
        account = self._origins.account.value
        document = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>oc-box — Private workspace</title>"
            "<h1>Private workspace</h1><p>Continue to the separate account site.</p>"
            f'<form method="post" action="{escape(account + ACCOUNT_ENTRY, quote=True)}">'
            f'<input type="hidden" name="ticket" value="{escape(issued.value, quote=True)}">'
            '<button type="submit">Continue to sign in</button></form></html>'
        ).encode()
        return WorkspaceEntry(
            document,
            _cookie(_APP_BOOTSTRAP, issued.app_nonce),
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action "
            + account,
        )

    def account(self, *, method, target, headers, body, actual_tls):
        if (
            method != "POST"
            or target != ACCOUNT_ENTRY
            or type(body) is not bytes
            or len(body) != 50
        ):
            _refuse()
        fields = self._document_fields(
            origin=self._origins.account,
            form_origin=self._origins.workspace,
            headers=headers,
            method=method,
            actual_tls=actual_tls,
        )
        if (
            fields.get("content-length") != "50"
            or fields.get("content-type") != "application/x-www-form-urlencoded"
            or (ticket := _TICKET.fullmatch(body)) is None
        ):
            _refuse()
        self._live()
        nonce = self._bootstrap.accept(
            self._binding,
            auth_origin=self._origins.account.value,
            ticket=ticket[1].decode("ascii"),
        )
        self._live()
        return AccountEntry(_cookie(_ACCOUNT_BOOTSTRAP, nonce))
