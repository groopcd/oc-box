"""Disposable two-site TLS, real browser PAKE and a pinned local native service."""

import argparse
import base64
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import socket
from socketserver import BaseRequestHandler, TCPServer, ThreadingMixIn
import ssl
import struct
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Thread
from time import monotonic
from traceback import extract_tb
from unittest.mock import patch

from playwright.sync_api import Error as BrowserError, sync_playwright

from oc_box import (
    web_account_ingress,
    web_accounts,
    web_admission,
    web_app_ingress,
    web_bootstrap,
    web_handoff,
    web_local_origins,
    web_named_bootstrap,
    web_named_handoff,
    web_named_headers,
    web_named_local,
    web_named_transport,
    web_native_pair,
    web_pake_flow,
    web_pake_profile,
)
from named_browser_native_fixture import native_fixture
from smoke_named_bootstrap_browser import material, require, run

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "tools/web-pake-research"
PAGE = ROOT / "tests/fixtures/named_browser"
ACCOUNT_COOKIE = "__Host-ocbox-account"
WORKSPACE_COOKIE = "__Host-ocbox-bootstrap"
USERNAME = "synthetic-browser"
VECTOR_SIZE = 37
ASSETS = "/account-assets/"
FIXED = (
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "origin"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
REFUSED = (403, b"Refused", FIXED + (("Content-Type", "text/plain"),))
HANDOFF = web_named_handoff.HANDOFF_PATH
CONFIRM = web_named_handoff.CONFIRM_PATH
OPAQUE_DOCUMENT = "data:text/html,<!doctype html><title>Foreign fixture</title>"
SLOT_SHA = "e8272c8e4e3bc675afa5325cefef3493864681d44dcd6144959e2811a3cb0c34"


def loopback_namespace():
    require(
        os.geteuid() != 0 and [name for _, name in socket.if_nameindex()] == ["lo"],
        "Run as non-root with only loopback in a disposable network namespace.",
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        flags = fcntl.ioctl(probe, 0x8913, struct.pack("16s24x", b"lo"))  # SIOCGIFFLAGS
    require(struct.unpack_from("H", flags, 16)[0] & 1, "Private loopback must be up.")


def read_fixture_request(stream, deadline):
    """Only for this test: bounded, raw-header HTTP/1.1 over the accepted TLS peer."""
    header_limit = 16_384
    wire = bytearray()
    while (end := wire.find(b"\r\n\r\n")) < 0:
        if len(wire) == header_limit:
            raise web_named_headers.NamedRequestError()
        wire.extend(
            web_named_transport._recv(
                stream, min(4096, header_limit - len(wire)), deadline
            )
        )
    lines = bytes(wire[:end]).split(b"\r\n", 65)
    if len(lines) > 65 or not all(line.isascii() for line in lines):
        raise web_named_headers.NamedRequestError()
    start = re.fullmatch(rb"(GET|POST) ([!-~]{1,128}) HTTP/1\.1", lines[0])
    if start is None:
        raise web_named_headers.NamedRequestError()
    method, target = (item.decode("ascii") for item in start.groups())
    headers = []
    fields = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        key = name.decode("ascii").lower()
        if not sep or key in fields:
            raise web_named_headers.NamedRequestError()
        headers.append((name.decode("ascii"), value.decode("ascii")))
        fields[key] = value.strip(b" ")
    if fields.keys() & {
        "transfer-encoding",
        "content-encoding",
        "expect",
        "upgrade",
        "trailer",
    }:
        raise web_named_headers.NamedRequestError()
    length = fields.get("content-length", b"0")
    if not re.fullmatch(rb"0|[1-9][0-9]{0,3}", length):
        raise web_named_headers.NamedRequestError()
    size = int(length)
    body = wire[end + 4 :]
    if size > 8192 or len(body) > size or (method == "GET" and size):
        raise web_named_headers.NamedRequestError()
    while len(body) < size:
        body.extend(web_named_transport._recv(stream, size - len(body), deadline))
    return dict(
        method=method,
        target=target,
        headers=headers,
        body=bytes(body),
        actual_tls=isinstance(stream, ssl.SSLSocket),
    )


class FixtureRequest(BaseRequestHandler):
    def handle(self):
        deadline = monotonic() + 9
        try:
            with self.server.tls.wrap_socket(
                self.request, server_side=True, do_handshake_on_connect=False
            ) as stream:
                stream.settimeout(web_named_transport._remaining(deadline))
                stream.do_handshake()
                request = read_fixture_request(stream, deadline)
                try:
                    status, body, headers = self.server.dispatch(
                        self.server.role, request
                    )
                except web_named_headers.NamedRequestError:
                    status, body, headers = REFUSED
                reason = {200: "OK", 204: "No Content", 403: "Forbidden"}[status]
                head = f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\n"
                if status != 204:
                    head += f"Content-Length: {len(body)}\r\n"
                head += "".join(
                    f"{key}: {value}\r\n"
                    for key, value in headers
                    if key != "Content-Length"
                )
                stream.settimeout(web_named_transport._remaining(deadline))
                stream.sendall((head + "\r\n").encode("ascii") + body)
        except (OSError, web_named_headers.NamedRequestError):
            # Idle/speculative connections and untrusted TLS probes can end
            # without a request. Do not retain wire, peer or browser errors.
            pass


class FixtureServer(ThreadingMixIn, TCPServer):
    request_queue_size = 8
    daemon_threads = False  # server_close joins the bounded connections.

    def __init__(self, role):
        self.role = role
        self.failed = False
        self.slots = BoundedSemaphore(6)
        super().__init__(("127.0.0.1", 0), FixtureRequest)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.failed = True
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        self.failed = True  # Never print a handler traceback.

    @contextmanager
    def serving(self, tls, dispatch):
        self.tls, self.dispatch = tls, dispatch
        self.tls.set_alpn_protocols(["http/1.1"])
        worker = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05})
        worker.start()
        try:
            yield
        finally:
            self.shutdown()
            self.server_close()
            worker.join(timeout=5)
            require(
                not worker.is_alive() and not self.failed,
                "Fixture listener did not drain cleanly.",
            )


class Composition:
    def __init__(
        self, origins, binding, bootstrap, sign_in, assets, native, owner_current
    ):
        self.origins = origins
        self.binding = binding
        self.bootstrap = bootstrap
        self.sign_in = sign_in
        self.assets = assets
        self.native = native
        self.entry = web_named_bootstrap.NamedBootstrapIngress(
            origins=origins,
            binding=binding,
            bootstrap=bootstrap,
            owner_current=owner_current,
        )
        self.app = web_app_ingress.NamedAppIngress(
            sign_in,
            endpoint=native.endpoint,
            version=native.identity["version"],
            claim_bootstrap=lambda nonce: bootstrap.claim_app(binding, nonce),
        )
        self.handoff = web_named_handoff.NamedHandoffIngress(
            origins=origins, app=self.app
        )
        self.pake = web_account_ingress.AccountIngress(
            origins=origins,
            sign_in=sign_in,
            translate_nonce=self.translate,
        )
        self.page = (
            (PAGE / "account.html")
            .read_bytes()
            .replace(b"__ACCOUNT_ORIGIN__", origins.account.value.encode("ascii"))
            .replace(b"__WORKSPACE_ORIGIN__", origins.workspace.value.encode("ascii"))
        )
        self.traffic = []
        self.ticket = None

    def translate(self, nonce):
        return self.bootstrap.app_nonce(
            self.binding,
            auth_origin=self.origins.account.value,
            account_nonce=nonce,
        )

    def __call__(self, role, request):
        target = request["target"]
        method = request["method"]
        if (
            role == "workspace"
            and target == web_named_bootstrap.WORKSPACE_ENTRY
            and method == "GET"
        ):
            result = self.entry.workspace(**request)
            return 200, result.html, result.headers()
        if role == "workspace" and (method, target) in (
            ("POST", HANDOFF),
            ("POST", CONFIRM),
        ):
            handle = self.handoff.receive if target == HANDOFF else self.handoff.confirm
            result = handle(**request)
            return result.status, result.body, result.headers
        if role == "workspace" and (method, target) == ("GET", "/"):
            return self.landing(request)
        if (
            role == "account"
            and target == web_named_bootstrap.ACCOUNT_ENTRY
            and method == "POST"
        ):
            result = self.entry.account(**request)
            return (
                200,
                self.page,
                FIXED
                + (
                    ("Content-Type", "text/html; charset=utf-8"),
                    (
                        "Content-Security-Policy",
                        web_named_handoff.account_policy(self.origins),
                    ),
                    ("Set-Cookie", result.cookie),
                ),
            )
        if role == "account" and method == "GET" and target in self.assets:
            nonce = web_named_headers.admit_headers(
                origin=self.origins.account,
                headers=request["headers"],
                method=method,
                actual_tls=request["actual_tls"],
                cookie_name=ACCOUNT_COOKIE,
            )
            if self.translate(nonce) is not None:
                content_type, body = self.assets[target]
                return 200, body, FIXED + (("Content-Type", content_type),)
        if role == "account" and method == "POST" and target in ("/start", "/finish"):
            self.check_wire(request)
            result = self.pake(**request)
            self.traffic.append((target, result.status))
            if target == "/finish" and result.status == 200:
                reply = json.loads(result.body)
                require(
                    tuple(reply) == ("ticket",) and self.ticket is None,
                    "Unexpected successful finish envelope.",
                )
                self.ticket = reply["ticket"]  # Private test oracle only.
            return result.status, result.body, result.headers
        return REFUSED

    def check_wire(self, request):
        try:
            headers = dict(
                (key.lower(), value.strip()) for key, value in request["headers"]
            )
            cookie = web_named_headers._cookie(headers.get("cookie"), ACCOUNT_COOKIE)
            require(
                headers["cookie"] == f"{ACCOUNT_COOKIE}={cookie}"
                and self.translate(cookie)
                and headers["origin"] == self.origins.account.value
                and headers["sec-fetch-site"] == "same-origin",
                "Real account cookie or same-origin context missing.",
            )
            raw = request["body"]
            payload = web_account_ingress._inputs(request["target"], raw)
            if request["target"] == "/start":
                names, size = ("username", "ke1"), 96
                require(payload["username"] == USERNAME, "Unexpected account.")
            else:
                names, size = ("challenge_id", "ke3"), 64
            proof = base64.b64decode(payload[names[1]], validate=True)
            require(
                tuple(payload) == names
                and len(proof) == size
                and raw == json.dumps(payload, separators=(",", ":")).encode("ascii"),
                "Unexpected PAKE payload.",
            )
            # Test only the public seed vectors, never derive a real password.
            for wrong in (False, True):
                vector = bytes(65 + i % 26 for i in range(VECTOR_SIZE))
                if wrong:
                    vector = b"Z" + vector[1:]
                require(
                    all(
                        secret not in data
                        for secret in (vector, base64.b64encode(vector))
                        for data in (raw, proof)
                    ),
                    "Synthetic password reached HTTP.",
                )
        except Exception:
            raise RuntimeError("Account wire audit failed.") from None

    def take_ticket(self):
        ticket, self.ticket = self.ticket, None
        require(ticket is not None, "No real PAKE ticket was issued.")
        return ticket

    def landing(self, request):
        # A fixed authenticated marker only. This is not Studio or a native relay.
        token = web_named_headers.admit_headers(
            origin=self.origins.workspace,
            headers=request["headers"],
            method="GET",
            actual_tls=request["actual_tls"],
            cookie_name=web_app_ingress.APP_COOKIE_NAME,
        )
        try:
            session = self.app._session(token)
        except web_app_ingress.AppRequestError:
            return REFUSED
        require(session.principal == USERNAME, "Unexpected landing principal.")
        return (
            200,
            b"<!doctype html><h1>Disposable workspace admitted</h1>",
            FIXED
            + (
                ("Content-Type", "text/html; charset=utf-8"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                    f"form-action 'none'; connect-src {self.origins.workspace.value}{CONFIRM}",
                ),
            ),
        )


def browser_home(root, name, ca=None):
    home = root / name
    database = home / ".pki/nssdb"
    database.mkdir(mode=0o700, parents=True)
    run("certutil", "-N", "--empty-password", "-d", f"sql:{database}", cwd=home)
    if ca is not None:
        run(
            "certutil",
            "-A",
            "-d",
            f"sql:{database}",
            "-n",
            "Synthetic oc-box test CA",
            "-t",
            "C,,",
            "-i",
            str(ca),
            cwd=home,
        )
    return home


def launch(pw, chrome, home, hosts):
    return pw.chromium.launch(
        executable_path=chrome,
        chromium_sandbox=True,
        timeout=10_000,
        env=dict(
            HOME=str(home),
            TMPDIR=str(home),
            PATH="/usr/bin:/bin",
            LC_ALL="C.UTF-8",
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_CACHE_HOME=str(home / ".cache"),
        ),
        args=[
            "--disable-setuid-sandbox",
            "--disable-background-networking",
            "--no-proxy-server",
            "--host-resolver-rules="
            + ", ".join(f"MAP {host} 127.0.0.1" for host in hosts)
            + ", MAP * ~NOTFOUND",
        ],
    )


def bootstrap_page(page, workspace, account, *, replay=None):
    response = page.goto(workspace, timeout=10_000)
    require(
        response is not None and response.ok and page.evaluate("isSecureContext"),
        "Trusted workspace bootstrap did not load.",
    )
    ticket = page.locator('input[name="ticket"]')
    if replay is not None:
        ticket.evaluate("(field, token) => field.value = token", replay)
    value = ticket.input_value()
    with page.expect_response(
        lambda result: result.url == account, timeout=10_000
    ) as response:
        page.get_by_role("button", name="Continue to sign in").click(timeout=10_000)
    require(
        response.value.status == (200 if replay is None else 403),
        "Unexpected account bootstrap result.",
    )
    return value


def cookies(context, origins, *, admitted=False):
    result = {(item["domain"], item["name"]): item for item in context.cookies()}
    expected = {
        (origins.account.hostname, ACCOUNT_COOKIE),
        (origins.workspace.hostname, WORKSPACE_COOKIE),
    }
    if admitted:
        expected.add((origins.workspace.hostname, web_app_ingress.APP_COOKIE_NAME))
    require(
        set(result) == expected
        and all(
            item["secure"]
            and item["httpOnly"]
            and item["sameSite"] == "Strict"
            and item["path"] == "/"
            for item in result.values()
        ),
        "Bootstrap cookie scope changed.",
    )
    app = result[(origins.workspace.hostname, WORKSPACE_COOKIE)]["value"]
    account = result[(origins.account.hostname, ACCOUNT_COOKIE)]["value"]
    require(app != account, "Bootstrap cookies must differ.")
    if admitted:
        token = result[(origins.workspace.hostname, web_app_ingress.APP_COOKIE_NAME)][
            "value"
        ]
        require(token not in (app, account), "App cookie matched a bootstrap cookie.")
        return app, account, token
    return app, account


def wait_state(page, state):
    page.locator(f'#login-form[data-state="{state}"]').wait_for(timeout=10_000)


def submit(page, wrong):
    # Derive the public fixture vector in the renderer; don't pass it through
    # automation arguments, logs, static HTML or served JavaScript.
    field = page.locator("#login-password")
    try:
        prepared = field.evaluate(
            """(field, args) => {
                const user = document.getElementById("login-user");
                if (field.type !== "password" || field.disabled || user.disabled) return false;
                user.value = args.user;
                field.value = Array.from({ length: args.size }, (_, i) =>
                  String.fromCharCode(i === 0 && args.wrong ? 90 : 65 + i % 26)).join("");
                return true;
            }""",
            {"user": USERNAME, "size": VECTOR_SIZE, "wrong": wrong},
        )
        require(prepared, "The masked form was not ready.")
        page.get_by_role("button", name="Sign in", exact=True).click()
    except BrowserError:
        raise RuntimeError("Synthetic form interaction did not complete.") from None
    expected = "refused" if wrong else "verified"
    try:
        wait_state(page, expected)
    except BrowserError:
        raise RuntimeError(f"Synthetic form did not reach {expected}.") from None
    require(
        field.evaluate("element => element.value === ''"),
        "Web password field was not cleared.",
    )


def transfer(page, url, *, expected, copied=None):
    with page.expect_response(
        lambda response: response.url == url, timeout=10_000
    ) as pending:
        if copied is None:
            page.get_by_role("button", name="Continue to workspace", exact=True).click()
        else:
            # Only negative cases copy a real token through the private
            # automation pipe. Do not retain, print or use it to fake a proof.
            page.evaluate(
                """({url, ticket}) => {
              const form = document.createElement("form");
              form.method = "post";
              form.action = url;
              const field = document.createElement("input");
              field.type = "hidden"; field.name = "ticket"; field.value = ticket;
              form.append(field);
              document.body.append(form);
              try { form.submit(); }
              finally { field.value = ""; form.remove(); }
            }""",
                {"url": url, "ticket": copied},
            )
    response = pending.value
    require(
        response.status == expected and response.header_value("set-cookie") is None,
        "Unexpected handoff document result or cookie.",
    )
    page.wait_for_url(url, wait_until="load")
    if expected == 200:
        page.get_by_role(
            "button", name="Confirm workspace sign-in", exact=True
        ).wait_for()
        require(
            page.evaluate("""() => location.search === "" && location.hash === "" &&
          document.cookie === "" && !document.querySelector("script,input,form")"""),
            "The confirmation page exposed a ticket or cookie to its DOM.",
        )


def click_confirm(page, url, expected):
    with page.expect_response(
        lambda response: response.url == url, timeout=10_000
    ) as pending:
        page.get_by_role("button", name="Confirm workspace sign-in", exact=True).click()
    response = pending.value
    require(response.status == expected, "Unexpected same-origin confirmation result.")
    if expected == 403:
        require(response.header_value("set-cookie") is None, "Refusal issued a cookie.")
        page.get_by_text("Named sign-in refused.", exact=False).wait_for()
    return response


def check_native_boundary(fixture, token, confirmed):
    session = fixture.sign_in.admission.lookup(fixture.binding, token)
    require(
        session is not None, "The app did not retain the confirmed browser session."
    )
    origin, native = fixture.origins.workspace, fixture.native
    cookie = session.native_cookie.upstream_cookie(
        binding=fixture.binding,
        endpoint=native.endpoint,
        origin=origin,
        version=native.identity["version"],
    )
    header = confirmed.header_value("set-cookie")
    require(
        header
        == f"{web_app_ingress.APP_COOKIE_NAME}={token}; Secure; HttpOnly; SameSite=Strict; Path=/"
        and cookie.split("=", 1)[1] not in header
        and confirmed.header_value("content-length") is None,
        "The native credential leaked or the browser cookie contract changed.",
    )
    reply = web_native_pair._exchange(
        web_native_pair._port(native.endpoint),
        origin.value.removeprefix("https://"),
        monotonic() + 4,
        "/api/info",
        cookie=cookie,
    )
    require(
        web_native_pair._json_reply(reply) == native.identity,
        "The retained private cookie did not authenticate the pinned native service.",
    )


def browser_checks(pw, chrome, homes, hosts, fixture, product):
    origins = fixture.origins
    workspace = origins.workspace.value + web_named_bootstrap.WORKSPACE_ENTRY
    account = origins.account.value + web_named_bootstrap.ACCOUNT_ENTRY
    handoff, confirm, landing = (
        origins.workspace.value + path for path in (HANDOFF, CONFIRM, "/")
    )
    untrusted = launch(pw, chrome, homes[0], hosts)
    try:
        with untrusted.new_context() as context:
            page = context.new_page()
            for url in (workspace, account):
                try:
                    page.goto(url, timeout=10_000)
                except BrowserError as exc:
                    require(
                        "net::ERR_CERT_AUTHORITY_INVALID" in str(exc),
                        "Unexpected TLS trust refusal.",
                    )
                else:
                    raise RuntimeError("Untrusted synthetic CA was accepted.")
    finally:
        untrusted.close()
    print("PASS fresh untrusted NSS profile refused both sites", flush=True)

    browser = launch(pw, chrome, homes[1], hosts)
    try:
        require(
            browser.version == product.removeprefix("HeadlessChrome/"),
            "Browser product mismatch.",
        )
        with browser.new_context() as context, browser.new_context() as other:
            allowed = {
                ("GET", workspace),
                ("GET", landing),
                ("GET", OPAQUE_DOCUMENT),
                ("POST", handoff),
                ("POST", confirm),
                ("POST", account),
                ("POST", origins.account.value + "/start"),
                ("POST", origins.account.value + "/finish"),
                ("GET", origins.account.value + "/favicon.ico"),
                ("GET", origins.workspace.value + "/favicon.ico"),
                *(("GET", origins.account.value + path) for path in fixture.assets),
            }
            unexpected = False

            def audit_url(request):
                nonlocal unexpected
                unexpected |= (request.method, request.url) not in allowed

            context.on("request", audit_url)
            other.on("request", audit_url)
            page = context.new_page()
            page.set_default_timeout(10_000)
            without_cookie = page.goto(origins.account.value + ASSETS + "client.wasm")
            require(
                without_cookie is not None and without_cookie.status == 403,
                "An unpaired browser received the PAKE asset.",
            )
            first = bootstrap_page(page, workspace, account)
            wait_state(page, "ready")
            require(
                page.url == account
                and page.evaluate("isSecureContext && document.cookie === ''"),
                "Account page context or HttpOnly boundary changed.",
            )
            app, account_cookie = cookies(context, origins)
            require(
                fixture.translate(account_cookie) == app, "Bootstrap was not paired."
            )
            second = other.new_page()
            bootstrap_page(second, workspace, account, replay=first)
            bootstrap_page(second, workspace, account)
            wait_state(second, "ready")
            other_app, other_account = cookies(other, origins)
            require(
                fixture.translate(other_account) == other_app,
                "Second bootstrap was not paired.",
            )
            print(
                "PASS real cross-site POST, host-only cookies, and one-use bootstrap replay refusal",
                flush=True,
            )

            # Attempt native submission only in the sacrificial second page.
            # Chromium can leave an aborted navigation on that frame even
            # when its enforced CSP blocked the request.
            no_fallback = second.evaluate("""() => {
              const form = document.getElementById("login-form");
              if (Array.from(form.elements).some(field => field.hasAttribute("name"))) return false;
              return new Promise(resolve => {
                const timer = setTimeout(() => resolve(false), 2000);
                document.addEventListener("securitypolicyviolation", event => {
                  clearTimeout(timer);
                  resolve(event.effectiveDirective === "form-action");
                }, { once: true });
                form.submit();
              });
            }""")
            require(
                no_fallback and second.url == account and not fixture.traffic,
                "Native form submission was not blocked.",
            )
            submit(page, wrong=True)
            require(
                fixture.traffic == [("/start", 200)]
                and fixture.ticket is None
                and page.url == account,
                "Wrong password emitted proof or changed the URL.",
            )
            submit(page, wrong=False)
            require(
                fixture.traffic == [("/start", 200), ("/start", 200), ("/finish", 200)]
                and page.url == account
                and cookies(context, origins) == (app, account_cookie),
                "Matching login did not produce only a PAKE ticket.",
            )
            ticket = fixture.take_ticket()
            transfer(page, handoff, expected=200)
            require(
                page.url == handoff
                and cookies(context, origins) == (app, account_cookie)
                and fixture.native.pairs == 0,
                "Cross-site transfer admitted before confirmation.",
            )

            # A workspace document is the wrong Origin for cross-site transfer.
            foreign = other.new_page()
            response = foreign.goto(landing)
            require(
                response is not None and response.status == 403,
                "Unadmitted workspace opened.",
            )
            transfer(foreign, handoff, expected=403, copied=ticket)
            foreign.goto(OPAQUE_DOCUMENT)
            require(
                foreign.evaluate("origin === 'null'"),
                "Expected the opaque foreign test document.",
            )
            transfer(foreign, handoff, expected=403, copied=ticket)
            foreign.close()

            # The right account origin can transfer a copied token, but the
            # confirmation is authorized by the real workspace bootstrap only.
            bootstrap_page(second, workspace, account)
            wait_state(second, "ready")
            other_app, other_account = cookies(other, origins)
            require(
                other_app != app, "The second browser did not get a distinct bootstrap."
            )
            transfer(second, handoff, expected=200, copied=ticket)
            click_confirm(second, confirm, 403)
            require(
                cookies(other, origins) == (other_app, other_account)
                and fixture.native.pairs == 0,
                "The wrong browser attempted native issuance.",
            )

            confirmed = click_confirm(page, confirm, 204)
            page.wait_for_url(landing)
            page.get_by_role("heading", name="Disposable workspace admitted").wait_for()
            _, _, app_token = cookies(context, origins, admitted=True)
            check_native_boundary(fixture, app_token, confirmed)
            response_status = page.evaluate(
                """({url, ticket}) => fetch(url, {
              method: "POST", mode: "same-origin", credentials: "same-origin",
              headers: {"Content-Type": "text/plain"}, body: ticket,
            }).then(response => response.status)""",
                {"url": confirm, "ticket": ticket},
            )
            require(
                response_status == 403 and fixture.native.pairs == 1,
                "Confirmation replay was accepted.",
            )

            bootstrap_page(second, workspace, account)
            wait_state(second, "ready")
            transfer(second, handoff, expected=403, copied=ticket)
            bootstrap_page(second, workspace, account)
            wait_state(second, "ready")
            submit(second, wrong=False)
            expired = fixture.take_ticket()
            transfer(second, handoff, expected=200)
            # Advance only the server-side handoff clock after the actual TLS
            # receiver accepted the fresh PAKE ticket. No real-time wait.
            with patch.object(web_handoff, "monotonic", new=lambda: monotonic() + 31):
                click_confirm(second, confirm, 403)
                bootstrap_page(second, workspace, account)
                wait_state(second, "ready")
                transfer(second, handoff, expected=403, copied=expired)
            require(
                fixture.native.pairs == 1
                and fixture.traffic
                == [
                    ("/start", 200),
                    ("/start", 200),
                    ("/finish", 200),
                    ("/start", 200),
                    ("/finish", 200),
                ],
                "Wrong, replayed or expired input opened extra native work.",
            )
            require(
                not unexpected,
                "The browser requested a URL outside the exact fixture routes.",
            )
            print(
                "PASS no form fallback; real PAKE, document handoff and same-origin confirmation",
                flush=True,
            )
            print(
                "PASS foreign, wrong-browser, replay and expired refusals; native cookie stayed private",
                flush=True,
            )
    finally:
        browser.close()


def exercise(root, chrome, build_dir, native_binary):
    research = runpy.run_path(str(RESEARCH / "check_browser"))
    pinned = research["pinned"]
    with chrome.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == research["CHROME_SHA"],
            "Pinned Chromium executable mismatch.",
        )
    build = build_dir / "target"
    worker = pinned(build / "release/ocop-worker", 524_480, research["WORKER_SHA"])
    seed = pinned(build / "release/ocop-fixture", 559_216, research["SEED_SHA"])
    assets = {
        ASSETS + "transport.mjs": (
            "text/javascript",
            pinned(
                ROOT / "src/oc_box/web_pake_client.mjs", 12_182, research["ADAPTER_SHA"]
            ),
        ),
        ASSETS + "slot.mjs": (
            "text/javascript",
            pinned(RESEARCH / "browser_slot.mjs", 6_787, SLOT_SHA),
        ),
        ASSETS + "sign-in.mjs": (
            "text/javascript",
            (PAGE / "sign-in.mjs").read_bytes(),
        ),
        ASSETS + "sign-in.css": ("text/css", (PAGE / "sign-in.css").read_bytes()),
        ASSETS + "transfer.mjs": (
            "text/javascript",
            (ROOT / "src/oc_box/web_named_handoff_client.mjs").read_bytes(),
        ),
        ASSETS + "client.wasm": (
            "application/wasm",
            pinned(
                build / "wasm32-unknown-unknown/release/ocop-browser.wasm",
                204_179,
                research["WASM_SHA"],
            ),
        ),
    }
    hosts = {
        "account": web_local_origins.ACCOUNT_HOST,
        "workspace": web_local_origins.WORKSPACE_HOST,
    }
    ca, pairs = material(root, hosts)
    homes = browser_home(root, "untrusted"), browser_home(root, "trusted", ca)
    with ExitStack() as cleanup:
        native = cleanup.enter_context(native_fixture(root, native_binary))
        servers = [cleanup.enter_context(FixtureServer(role)) for role in hosts]
        origins = web_local_origins.LocalOrigins(
            *(server.server_address[1] for server in servers)
        )
        tls = web_local_origins.check_tls(origins, **pairs)
        binding = web_admission.WebBinding("a" * 32, "b" * 32, origins.workspace.value)
        bootstrap = web_bootstrap.BrowserBootstrap(origins=origins, binding=binding)
        cleanup.callback(bootstrap.close)
        seed_fd = cleanup.enter_context(research["sealed"](seed))
        setup, record = research["seed_account"](
            seed_fd, origins, USERNAME, VECTOR_SIZE
        )
        store = web_accounts.AccountStore(
            setup, (web_accounts.Account(USERNAME, (binding.workspace_id,), record),)
        )
        worker_fd = cleanup.enter_context(research["sealed"](worker))
        backend = web_pake_profile.LocalPakeProfile(
            worker_fd,
            expected_sha256=bytes.fromhex(research["WORKER_SHA"]),
            origins=origins,
        )
        cleanup.callback(
            lambda: require(backend.close(), "Worker child cleanup was not confirmed.")
        )
        flow = web_pake_flow.PakeFlow(
            binding,
            store=store,
            backend=backend,
            sizes=backend.sizes,
            auth_origin=origins.account.value,
        )
        owner_current, pair_native = native.bind(binding, origins.workspace)
        sign_in = web_named_local.LocalNamedSignIn(
            origins=origins,
            binding=binding,
            flow=flow,
            owner_current=owner_current,
            pair_native=pair_native,
        )
        fixture = Composition(
            origins, binding, bootstrap, sign_in, assets, native, owner_current
        )
        try:
            with ExitStack() as listeners:
                for server in servers:
                    listeners.enter_context(
                        server.serving(getattr(tls, server.role), fixture)
                    )
                with sync_playwright() as pw:
                    browser_checks(
                        pw, chrome, homes, hosts.values(), fixture, research["PRODUCT"]
                    )
                require(
                    native.pairs == 1 and len(backend._worker._active) == 1,
                    "Unexpected native issuance or retained verifier count.",
                )
        finally:
            sign_in.close()
            require(
                not flow.cleanup_failed and flow.active_verifiers == 0,
                "Sign-in teardown was not confirmed.",
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument(
        "--opencode", type=Path, required=True, help="already-reviewed OpenCode 2.3.2"
    )
    args = parser.parse_args()
    loopback_namespace()
    os.umask(0o077)
    with TemporaryDirectory(prefix="ocop-named-tls-") as directory:
        try:
            exercise(
                Path(directory),
                args.chromium.resolve(strict=True),
                args.build_dir.resolve(strict=True),
                args.opencode.resolve(strict=True),
            )
        except BrowserError as error:
            location = next(
                (
                    f"{frame.name}:{frame.lineno}"
                    for frame in reversed(extract_tb(error.__traceback__))
                    if frame.filename == __file__
                ),
                "browser",
            )
            raise RuntimeError(
                f"Chromium fixture failed or timed out at {location}."
            ) from None
    print(
        "PASS Chromium 159 real TLS, PAKE, handoff and private native pairing; temporary homes removed"
    )
    print(
        "Synthetic account and owner bridge only. No enrollment, live owner, Studio relay or deployment evidence."
    )


if __name__ == "__main__":
    main()
