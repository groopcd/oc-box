"""Unserved two-POST browser handoff for one bound local named workspace.

The trusted transport selects the workspace role from the bound socket and
exact TLS SNI, supplies bounded raw requests, and owns the final liveness
check before writes and draining.
No native relay, listener, launcher configuration, or guest fallback here.
"""

import re
import secrets
from dataclasses import dataclass

if __package__:
    from . import (
        web_app_ingress,
        web_bootstrap,
        web_handoff,
        web_local_origins,
        web_named_headers,
    )
else:
    import web_app_ingress
    import web_bootstrap
    import web_handoff
    import web_local_origins
    import web_named_headers


HANDOFF_PATH = "/oc-box/handoff"
CONFIRM_PATH = web_app_ingress.CONFIRM_PATH
_FORM = re.compile(rb"ticket=([A-Za-z0-9_-]{53}[AQgw])")
_FORM_TYPE = "application/x-www-form-urlencoded"
_FIXED_HEADERS = (
    ("Cache-Control", "no-store"),
    # "no-referrer" can serialize Origin as null on a non-CORS POST.
    ("Referrer-Policy", "origin"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
_NO_FRAMING = frozenset(
    (
        "transfer-encoding",
        "content-encoding",
        "expect",
        "trailer",
        "te",
        "upgrade",
        "proxy-connection",
        "http2-settings",
        "range",
    )
)

# Only canonical base64url and a fixed route are substituted. The script node
# removes itself; the ticket is never a page element, URL, storage item or
# global property. The protected no-store POST response necessarily carries
# the opaque ticket back to this same browser's workspace document.
_CONFIRM_SCRIPT = """(() => {
  "use strict";
  document.currentScript.remove();
  if (!isSecureContext || window !== top || location.pathname !== "__HANDOFF__" ||
      location.search || location.hash || navigator.serviceWorker?.controller) return;
  const button = document.getElementById("confirm");
  const status = document.getElementById("status");
  const cancel = new AbortController();
  let ticket = "__TICKET__";
  const refuse = () => {
    status.textContent = "Named sign-in refused. Start again from the workspace entry.";
  };
  // This is a local retention cap, not an extension of the server expiry.
  const expiry = setTimeout(() => {
    ticket = "";
    button.disabled = true;
    refuse();
  }, 30000);
  addEventListener("pagehide", () => {
    clearTimeout(expiry);
    ticket = "";
    button.disabled = true;
    cancel.abort();
  }, { once: true });
  button.addEventListener("click", async event => {
    if (!event.isTrusted || !ticket || cancel.signal.aborted) return;
    clearTimeout(expiry);
    let body = ticket;
    ticket = "";
    button.disabled = true;
    status.textContent = "Confirming…";
    const url = location.origin + "__CONFIRM__";
    const timer = setTimeout(() => cancel.abort(), 10000);
    try {
      let pending;
      try {
        pending = fetch(url, {
          method: "POST", mode: "same-origin", credentials: "same-origin",
          redirect: "error", cache: "no-store", referrerPolicy: "origin",
          headers: { "Content-Type": "text/plain" }, body, signal: cancel.signal,
        });
      } finally { body = ""; }
      const response = await pending;
      if (cancel.signal.aborted || response.status !== 204 || response.type !== "basic" ||
          response.redirected || response.url !== url) throw new Error();
      location.replace("/");
    } catch { refuse(); }
    finally { clearTimeout(timer); }
  });
  button.disabled = false;
})();"""


def account_policy(origins):
    """Account document policy for the fixed public asset set and only this POST."""
    if type(origins) is not web_local_origins.LocalOrigins:
        raise ValueError("Expected the dedicated local HTTPS profile.")
    return (
        "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action "
        + origins.workspace.value
        + HANDOFF_PATH
    )


@dataclass(frozen=True, slots=True, repr=False)
class HandoffReply:
    """Only trusted, bounded document or empty confirmation response bytes."""

    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


class NamedHandoffIngress:
    """Separate transfer from admission; only confirmation can mint an app cookie."""

    def __init__(self, *, origins, app):
        if (
            type(origins) is not web_local_origins.LocalOrigins
            or type(app) is not web_app_ingress.NamedAppIngress
            or app._origin != origins.workspace
            or app._sign_in._auth_origin != origins.account.value
        ):
            raise ValueError("Invalid local browser handoff composition.")
        self._origins = origins
        self._app = app

    def _fields(self, *, method, headers, actual_tls, cross):
        origin = self._origins.workspace
        web_named_headers.admit_headers(
            origin=origin,
            headers=headers,
            method=method,
            actual_tls=actual_tls,
            form_origin=self._origins.account if cross else None,
        )
        fields = web_named_headers._fields(
            headers, origin.value.removeprefix("https://")
        )
        if (
            fields.keys() & _NO_FRAMING
            or any(field.startswith("sec-websocket-") for field in fields)
            or fields.get("connection", "close").lower() not in ("close", "keep-alive")
            or fields.get("sec-fetch-site")
            != ("cross-site" if cross else "same-origin")
            or fields.get("sec-fetch-mode") != ("navigate" if cross else "same-origin")
            or fields.get("sec-fetch-dest") != ("document" if cross else "empty")
        ):
            raise web_named_headers.NamedRequestError()
        return fields

    def receive(self, *, method, target, headers, actual_tls, body):
        """Validate one top-level opposite-site form; no cookie or state is issued."""
        if (
            type(method) is not str
            or method != "POST"
            or type(target) is not str
            or target != HANDOFF_PATH
            or type(body) is not bytes
            or len(body) != 61
        ):
            raise web_named_headers.NamedRequestError()
        fields = self._fields(
            method=method, headers=headers, actual_tls=actual_tls, cross=True
        )
        if (
            fields.get("content-type") != _FORM_TYPE
            or fields.get("content-length") != "61"
            or (match := _FORM.fullmatch(body)) is None
        ):
            raise web_named_headers.NamedRequestError()
        ticket = match[1].decode("ascii")
        if not self._app._sign_in.handoff_available(ticket):
            raise web_named_headers.NamedRequestError()
        try:
            nonce = secrets.token_hex(16)
        except OSError:
            raise web_named_headers.NamedRequestError() from None
        # Insert the canonical token last; its alphabet can itself contain a
        # template marker without changing the route or ticket.
        script = (
            _CONFIRM_SCRIPT.replace("__HANDOFF__", HANDOFF_PATH)
            .replace("__CONFIRM__", CONFIRM_PATH)
            .replace("__TICKET__", ticket)
        )
        # CSP denies all subresources and fallback forms; only this script can
        # transmit the handoff, at the exact same-origin confirmation path.
        policy = (
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
            f"script-src 'nonce-{nonce}'; connect-src {self._origins.workspace.value}{CONFIRM_PATH}"
        )
        html = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>oc-box — Confirm this browser</title><h1>Confirm this browser</h1>"
            '<button type="button" id="confirm" disabled>Confirm workspace sign-in</button>'
            '<p id="status" role="status"></p>'
            f'<script nonce="{nonce}">{script}</script></html>'
        ).encode("utf-8")
        return HandoffReply(
            200,
            html,
            _FIXED_HEADERS
            + (
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", policy),
            ),
        )

    def confirm_admission(self, *, method, target, headers, actual_tls, body):
        """Return the opaque app token to the trusted, tracked response writer."""
        if (
            type(method) is not str
            or method != "POST"
            or type(target) is not str
            or target != CONFIRM_PATH
            or type(body) is not bytes
            or len(body) != 54
        ):
            raise web_named_headers.NamedRequestError()
        fields = self._fields(
            method=method, headers=headers, actual_tls=actual_tls, cross=False
        )
        if (
            fields.get("content-type") != "text/plain"
            or fields.get("content-length") != "54"
            or not body.isascii()
            or not web_handoff._valid(body.decode("ascii"), 54, web_handoff._TOKEN)
        ):
            raise web_named_headers.NamedRequestError()
        try:
            token = self._app.confirm(
                method=method,
                target=target,
                headers=headers,
                actual_tls=actual_tls,
                body=body,
            )
        except web_app_ingress.AppRequestError:
            raise web_named_headers.NamedRequestError() from None
        if not web_bootstrap._valid(token):
            raise web_named_headers.NamedRequestError()
        return token

    def confirm(self, *, method, target, headers, actual_tls, body):
        """Fixed confirmation for direct fixtures; live delivery must track admission."""
        token = self.confirm_admission(
            method=method,
            target=target,
            headers=headers,
            actual_tls=actual_tls,
            body=body,
        )
        return HandoffReply(
            204,
            b"",
            _FIXED_HEADERS
            + (
                (
                    "Set-Cookie",
                    f"{web_app_ingress.APP_COOKIE_NAME}={token}; Secure; HttpOnly; SameSite=Strict; Path=/",
                ),
            ),
        )
