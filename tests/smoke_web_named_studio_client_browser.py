"""Intercepted Chromium smoke for the internal named Studio browser client.

    PYTHONPATH=src uv run --offline --no-project --with playwright python -B \
      tests/smoke_web_named_studio_client_browser.py --chromium /path/to/chromium

Real browser and source assets, synthetic response bytes only. No listener,
certificate, OpenCode, provider, credentials or workspace.
"""

import argparse
from collections import deque
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from smoke_web_studio_shell_browser import RUNTIME, assets


ORIGIN = "https://workspace.ocbox-app.test:18443"
FOREIGN = "https://accounts.ocbox.test:18444"
CHANGE = "/oc-box/studio/selection"
READ = "/oc-box/studio/conversation?session=ses_alpha"
SEND = "/oc-box/studio/send"
PAGE = "A" * 43
CHAT = "application/json; charset=utf-8"
MODULE = """
import { createNamedStudioController } from "/fixture/web_named_studio_client.mjs";
const root = document.getElementById("web-shell");
const shell = window.ocBoxWebShell;
const frame = shell.frame, originalWindow = frame.contentWindow;
const boot = {page:"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", generation:0, sessionID:null};
let client = null, action = null;
const modes = [], originalFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (new URL(url, location.href).pathname.startsWith("/oc-box/studio/")) {
    const request = new Request(url, options);
    modes.push([request.mode, request.credentials, request.redirect, request.cache, request.referrerPolicy]);
  }
  return originalFetch(url, options);
};
const controls = document.createElement("nav");
for (const [label, choose] of [
  ["Fixture Choose Alpha", event => client.choose(event, "ses_alpha")],
  ["Fixture Home", event => client.home(event)],
  ["Fixture Invalid ID", event => client.choose(event, "ses_alpha?session=ses_other")],
  ["Fixture One Click", event => {
    const first = client.choose(event, "ses_alpha");
    return Promise.all([first, client.home(event)]);
  }],
]) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = label;
  btn.addEventListener("click", event => { action = choose(event); });
  controls.append(btn);
}
root.prepend(controls);
window.fixture = {
  attach: (value = boot) => !!(client = createNamedStudioController(root, value)),
  action: () => action,
  present: () => client.present(),
  startPresent: () => { action = client.present(); },
  revoke: () => client.invalidate(),
  modes: () => modes,
  nativeUnchanged: () => frame === document.getElementById("native") &&
    frame.contentWindow === originalWindow && shell.selection()?.sessionID === "ses_alpha",
  api: () => Object.keys(client),
};
"""


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def snapshot(generation, session="ses_alpha"):
    return compact({"page": PAGE, "generation": generation, "sessionID": session})


def projection():
    return compact(
        {
            "sessionID": "ses_alpha",
            "title": "Synthetic named transcript",
            "hasEarlier": False,
            "messages": [
                {
                    "id": "msg_fixture",
                    "role": "user",
                    "text": "Literal <b>fixture</b> 🦉",
                    "truncated": False,
                }
            ],
        }
    )


class FixtureWire:
    def __init__(self):
        self.static = assets()
        mime, html = self.static["/composed"]
        self.static["/named"] = (
            mime,
            html.replace(b"/fixture/controller.mjs", b"/fixture/named-controller.mjs"),
        )
        self.static["/fixture/named-controller.mjs"] = (
            "text/javascript",
            MODULE.encode(),
        )
        self.static["/fixture/web_named_studio_client.mjs"] = (
            "text/javascript",
            (RUNTIME / "web_named_studio_client.mjs").read_bytes(),
        )
        self.replies = deque()
        self.calls = []
        self.unexpected = []

    def respond(self, target, status, body, content_type=CHAT, **headers):
        self.replies.append((target, status, body, content_type, headers))

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        if url.path.startswith("/oc-box/studio/"):
            target = url.path + ("?" + url.query if url.query else "")
            self.calls.append(
                (target, request.method, request.all_headers(), request.post_data)
            )
            if (
                f"{url.scheme}://{url.netloc}" != ORIGIN
                or not self.replies
                or self.replies[0][0] != target
            ):
                self.unexpected.append(request.url)
                route.abort()
                return
            _, status, body, content_type, headers = self.replies.popleft()
            if status is None:
                # Held open only until the browser's own abort/timeout.
                return
            if status == 0:
                route.abort()
                return
            route.fulfill(
                status=status,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                    **headers,
                },
                body=body,
            )
        elif request.method == "GET" and not url.query and url.path in self.static:
            content_type, body = self.static[url.path]
            route.fulfill(content_type=content_type, body=body)
        else:
            self.unexpected.append(request.url)
            route.abort()

    def check_calls(self):
        for target, method, headers, body in self.calls:
            assert "authorization" not in headers and "referer" not in headers
            if target == CHANGE:
                assert method == "POST"
                assert (
                    headers["origin"] == ORIGIN
                    and headers["content-type"] == "application/json"
                )
                assert (
                    "x-oc-box-studio-page" not in headers
                    and "x-oc-box-studio-generation" not in headers
                )
                assert set(json.loads(body)) == {"page", "sequence", "sessionID"}
                assert json.loads(body)["page"] == PAGE
            else:
                assert headers["x-oc-box-studio-page"] == PAGE
                assert int(headers["x-oc-box-studio-generation"]) > 0
                assert headers["x-oc-box-studio-generation"] == str(
                    int(headers["x-oc-box-studio-generation"])
                )
                if target == READ:
                    assert (
                        method == "GET"
                        and "content-type" not in headers
                        and body is None
                    )
                else:
                    assert method == "POST" and target == SEND
                    assert (
                        headers["origin"] == ORIGIN
                        and headers["content-type"] == "application/json"
                    )
                    assert set(json.loads(body)) == {"sessionID", "text"}
                    assert json.loads(body)["sessionID"] == "ses_alpha"
        assert not self.replies and not self.unexpected


def exercise(browser, expect):
    wire = FixtureWire()
    errors = []
    context = browser.new_context(service_workers="block")
    try:
        context.set_default_timeout(5000)
        context.route("**/*", wire.route)

        def deny_socket(ws):
            wire.unexpected.append(ws.url)
            ws.close()

        context.route_web_socket("**/*", deny_socket)

        def open_page(origin):
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(error))
            page.goto(origin + "/named", wait_until="load")
            page.wait_for_function("window.fixture !== undefined")
            return page

        for origin in (FOREIGN, ORIGIN.replace("https:", "http:")):
            other = open_page(origin)
            assert not other.evaluate("fixture.attach()")
            other.close()

        page = open_page(ORIGIN)
        assert page.evaluate("window.isSecureContext")
        for value in (
            {"page": PAGE, "generation": 1, "sessionID": None},
            {"page": PAGE, "generation": 0, "sessionID": "ses_alpha"},
            {"page": PAGE, "generation": 0, "sessionID": None, "grant": "unsupported"},
            {"page": "not a page handle", "generation": 0, "sessionID": None},
        ):
            assert not page.evaluate("value => fixture.attach(value)", value)
        assert page.evaluate("fixture.attach()")
        assert page.evaluate("fixture.api()") == [
            "choose",
            "home",
            "present",
            "invalidate",
            "dispose",
        ]
        alias = page.get_by_role("button", name="Fixture Choose Alpha", exact=True)
        home = page.get_by_role("button", name="Fixture Home", exact=True)
        native = page.locator("#native")
        center = page.locator("#workspace-studio-center")
        field = center.get_by_role("textbox", name="Message", exact=True)
        send = center.get_by_role("button", name="Send", exact=True)
        status = center.get_by_role("status")
        fallback_draft = page.frame_locator("#native").locator("#draft")
        fallback_draft.fill("Do not touch this native fixture draft")

        def choose(
            generation, session="ses_alpha", control=alias, *, raw=None, accepted=True
        ):
            wire.respond(
                CHANGE,
                200,
                raw if raw is not None else snapshot(generation, session),
                "application/json",
            )
            control.click()
            assert page.evaluate("fixture.action()") == accepted

        def show(payload=None, accepted=True):
            wire.respond(READ, 200, projection() if payload is None else payload)
            assert page.evaluate("fixture.present()") is accepted
            if accepted:
                expect(
                    center.get_by_text("Literal <b>fixture</b> 🦉", exact=True)
                ).to_be_visible()
                expect(native).to_be_hidden()
            else:
                expect(native).to_be_visible()

        def send_result(code, payload, expected, **headers):
            field.fill("  One synthetic input 🦉\nline  ")
            send.evaluate("button => button.click()")
            wire.respond(SEND, code, payload, **headers)
            send.click()
            expect(status).to_contain_text(expected)
            assert (
                json.loads(wire.calls[-1][3])["text"]
                == "  One synthetic input 🦉\nline  "
            )

        expect(center).to_be_hidden()
        assert not page.evaluate("fixture.present()")
        alias.evaluate("button => button.click()")
        assert not page.evaluate("fixture.action()")
        page.get_by_role("button", name="Fixture Invalid ID").click()
        assert not page.evaluate("fixture.action()")
        page.evaluate("""window.dispatchEvent(new CustomEvent("oc-box:selection-changed", {
          detail: {sessionID:"ses_alpha", generation:1}
        }))""")
        assert wire.calls == []

        duplicate = snapshot(1).replace(
            b'"generation":1', b'"generation":1,"generation":1'
        )
        choose(1, raw=duplicate, accepted=False)
        assert not page.evaluate("fixture.present()")
        # A refused/undelivered action can leave the server generation behind
        # the browser's action sequence. One real click cannot be used twice.
        choose(
            1,
            control=page.get_by_role("button", name="Fixture One Click"),
            accepted=[True, False],
        )
        assert [json.loads(row[3])["sequence"] for row in wire.calls] == [1, 2]
        assert len(wire.calls) == 2  # present() is a separate readiness step.
        show()
        assert wire.calls[-1][2]["x-oc-box-studio-generation"] == "1"
        send_result(403, b'{"status":"refused"}', "Not sent")
        expect(send).to_be_enabled()
        send_result(200, b'{"status":"queued"}', "acknowledged")
        expect(field).to_have_value("")
        send_result(503, b'{"status":"unconfirmed"}', "may already be queued")
        expect(send).to_be_disabled()
        expect(field).to_have_value("  One synthetic input 🦉\nline  ")

        choose(2, None, home)
        expect(native).to_be_visible()
        assert page.evaluate("fixture.nativeUnchanged()")
        expect(fallback_draft).to_have_value("Do not touch this native fixture draft")
        assert not page.evaluate("fixture.present()")

        # A same-path redirect, foreign redirect or malformed status/body is
        # not a refusal; it cannot re-enable another Send on this generation.
        for gen, code, data, headers in (
            (3, 403, b'{"status":"refused","status":"refused"}', {}),
            (4, 200, b'{"status":"refused"}', {}),
            (5, 302, b"", {"Location": FOREIGN + SEND}),
            (6, 307, b"", {"Location": ORIGIN + SEND}),
            (7, 0, b"", {}),
            (8, 200, b" " * 513, {}),
        ):
            choose(gen)
            show()
            send_result(code, data, "Send unconfirmed", **headers)
            expect(send).to_be_disabled()

        choose(9)
        show(b"x" * (512 * 1024 + 1), accepted=False)
        choose(10)
        show(
            projection().replace(
                b'"truncated":false', b'"truncated":false,"truncated":false'
            ),
            accepted=False,
        )

        # Route invalidation aborts a live read. The new choice of the same ID
        # receives a distinct scope; it cannot resurrect the former draft.
        choose(11)
        wire.respond(READ, None, b"")
        with page.expect_request(lambda req: req.url == ORIGIN + READ):
            page.evaluate("fixture.startPresent()")
        with page.expect_event(
            "requestfailed", predicate=lambda req: req.url == ORIGIN + READ
        ):
            page.evaluate("window.dispatchEvent(new PopStateEvent('popstate'))")
        assert not page.evaluate("fixture.action()")
        expect(native).to_be_visible()
        assert not page.evaluate("fixture.present()")
        choose(12)
        show()
        expect(field).to_have_value("")
        field.fill("An admitted attempt can be uncertain")
        wire.respond(SEND, None, b"")
        with page.expect_request(lambda req: req.url == ORIGIN + SEND):
            send.click()
        with page.expect_event(
            "requestfailed", predicate=lambda req: req.url == ORIGIN + SEND
        ):
            page.evaluate("fixture.revoke()")
        expect(center).to_be_hidden()
        assert page.evaluate("fixture.nativeUnchanged()")

        choose(13)
        show()
        expect(field).to_have_value("")
        field.fill("Timed out receipt")
        wire.respond(SEND, None, b"")
        send.click()
        expect(status).to_contain_text("Send unconfirmed", timeout=9000)
        expect(send).to_be_disabled()
        # A newer explicit Home consumes its own sequence and aborts the
        # previous pending selection, even before a projection existed.
        wire.respond(CHANGE, None, b"")
        with page.expect_request(lambda req: req.url == ORIGIN + CHANGE):
            alias.click()
        page.evaluate("() => { window.heldSelection = fixture.action(); }")
        with page.expect_event(
            "requestfailed", predicate=lambda req: req.url == ORIGIN + CHANGE
        ):
            choose(14, None, home)
        assert not page.evaluate("window.heldSelection")
        assert not page.evaluate("fixture.present()")
        expect(center).to_be_hidden()
        assert len(page.evaluate("Object.keys(localStorage)")) == 0
        assert len(page.evaluate("Object.keys(sessionStorage)")) == 0
        # Browser routing does not expose Chromium's final Sec-Fetch-* wire
        # headers. It does expose the actual Request mode and policy options.
        assert page.evaluate("fixture.modes()") == [
            ["same-origin", "same-origin", "error", "no-store", "no-referrer"]
        ] * len(wire.calls)
        assert not errors
        wire.check_calls()
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path, required=True)
    options = parser.parse_args()
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        parser.error("Cached Playwright is required; see --help")
    chromium = options.chromium.resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="oc-box-named-studio-browser-"
    ) as directory:
        home = Path(directory)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "TMPDIR": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
        }
        with patch.dict(os.environ, env, clear=True), sync_playwright() as driver:
            browser = driver.chromium.launch(
                executable_path=chromium,
                headless=True,
                env=env,
                timeout=15_000,
                args=[
                    "--disable-background-networking",
                    "--no-proxy-server",
                    "--host-resolver-rules=MAP * ~NOTFOUND",
                ],
            )
            try:
                version = browser.version
                exercise(browser, expect)
            finally:
                browser.close()
    print(
        f"PASS Chromium {version}: exact named Studio browser envelopes and bounded lifecycle."
    )
    print(
        "PASS trusted gestures, aborts, strict Send results, no fallback frame mutation or replay."
    )


if __name__ == "__main__":
    main()
