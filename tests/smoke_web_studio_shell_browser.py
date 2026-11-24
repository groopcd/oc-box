"""Synthetic Chromium smoke of the internally opted-in outer Studio shell.

    PYTHONPATH=src uv run --offline --no-project --with playwright python -B \
      tests/smoke_web_studio_shell_browser.py --chromium /path/to/chromium

Interception only; no actual shell route, native process, or provider.
"""

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from oc_box import web_sessions

from smoke_web_studio_composer_browser import APPROVED_SHA256, ROOT, RUNTIME, VIEWPORT
from test_web_studio_shell import URL, fixture_document, fixture_state


ORIGIN = "https://studio-shell-fixture.invalid"
CENTER = "#workspace-studio-center"
MODULE = """
import { createStudioShellView } from "/fixture/web_studio_shell.mjs";
const root = document.getElementById("web-shell");
const shell = window.ocBoxWebShell;
const frame = shell.frame, nativeWindow = frame.contentWindow;
const nativeAttributes = () => [...frame.attributes].map(a => [a.name, a.value]);
const original = nativeAttributes();
const reads = [], sends = [];
let selection = null, generation = 0, bridge = null, showing = null;
let readReply = null, sentReply = null;
const controller = {
  readSelection: () => selection,
  readTranscript: scope => {
    if (readReply || reads.length >= 12) throw new Error("Too many fixture reads");
    reads.push(scope);
    return new Promise(resolve => { readReply = resolve; });
  },
  send: action => {
    if (sentReply || sends.length >= 4) throw new Error("Too many fixture sends");
    sends.push(action);
    return new Promise(resolve => { sentReply = resolve; });
  },
};
function invalidate() {
  selection = null;
  bridge?.invalidate();
}
// Events are invalidation hints only. Grants below are explicit fixture calls,
// never lifted from these events or a catalog.
window.addEventListener("oc-box:selection-changed", invalidate);
window.addEventListener("pagehide", invalidate);
document.addEventListener("visibilitychange", () => { if (document.hidden) invalidate(); });
window.fixture = {
  attach: mode => !!(bridge = createStudioShellView(root, mode === "missing" ? null : controller)),
  grant: (sessionID, writable) => { selection = {sessionID, generation: ++generation, writable}; },
  start: () => { showing = bridge.present(); },
  probe: () => bridge.present(),
  presented: () => showing,
  invalidate,
  clearWithoutAdvancing: () => bridge.invalidate(),
  reads: () => reads,
  sends: () => sends,
  nativeSelection: () => shell.selection()?.sessionID ?? null,
  nativeUnchanged: () => frame === document.getElementById("native") &&
    original.every(([key, value]) => frame.getAttribute(key) === value) &&
    frame.attributes.length === original.length && frame.contentWindow === nativeWindow,
  deliver: valid => {
    if (!readReply) throw new Error("No fixture read");
    const resolve = readReply, scope = reads.at(-1);
    readReply = null;
    resolve({
      sessionID: valid ? scope.sessionID : "ses_wrong",
      title: "Synthetic approved projection",
      hasEarlier: false,
      messages: [{ id: "msg_fixture", role: "user", text: "First-party fixture text", truncated: false }]
    });
  },
  settleSend: result => {
    if (!sentReply) throw new Error("No fixture send");
    const resolve = sentReply;
    sentReply = null;
    resolve(result);
  },
};
"""
NAMED_MODULE = """
import { createStudioShellView } from "/oc-box/studio/web_studio_shell.mjs";
const root = document.getElementById("web-shell");
const verifies = [], reads = [], sends = [], catalogs = [], cancelled = [];
let verifyReply, readReply, chrome, view, showing, scope = null, generation = 0;
const controller = {
  readSelection: () => scope,
  readTranscript: ticket => {
    reads.push(ticket);
    return new Promise(resolve => { readReply = resolve; });
  },
  send: action => { sends.push(action); return "refused"; },
};
const adapter = {
  verifySelection: (id, {signal}) => {
    verifies.push(id);
    signal.addEventListener("abort", () => cancelled.push(id), {once:true});
    return new Promise(resolve => { verifyReply = resolve; });
  },
  selectionChanged: selected => {
    if (selected === null) {
      scope = null;
      view?.invalidate();
    } else showing = view.present();
  },
  readCatalog: ({signal}) => {
    catalogs.push(signal.aborted);
    return {status:"valid", sessions:[
      {id:"ses_alpha", title:"Synthetic <alpha>"},
      {id:"ses_beta", title:"Synthetic Beta"},
    ]};
  },
};
window.namedFixture = {
  attach: () => {
    chrome = window.ocBoxMountNamedStudioChrome(adapter);
    view = createStudioShellView(root, controller);
    return !!view;
  },
  bareMount: () => createStudioShellView(root, controller) === null,
  noUntrustedMount: () => window.ocBoxMountNamedStudioChrome({}) === null,
  verify: (accepted, sessionID, writable) => {
    scope = accepted ? {sessionID, generation:++generation, writable} : null;
    verifyReply(accepted);
  },
  deliver: id => readReply({
    sessionID: id, title: "Synthetic approved projection", hasEarlier: false,
    messages: [{ id:"msg_named", role:"user", text:"Named fixture text", truncated:false }],
  }),
  status: () => ({verifies, reads, sends, catalogs, cancelled, selected:chrome?.selection()}),
  presented: () => showing,
};
"""

GEOMETRY = """ids => {
  const [chat, ...panes] = ids.map(id => document.querySelector(id));
  const box = el => el.getBoundingClientRect();
  const layout = [chat, ...panes].map(el => [box(el).left, box(el).width]);
  const composer = box(chat.querySelector(".compose")), base = box(chat);
  const header = box(chat.querySelector(".chat-head, .workspace-chat-head"));
  return {layout, relative: [composer.left - base.left, composer.top - header.bottom,
    composer.width, composer.height]};
}"""


def assets():
    state = fixture_state()
    approved = (ROOT / "docs/mockups/web-manager.html").read_bytes()
    assert hashlib.sha256(approved).hexdigest() == APPROVED_SHA256
    html = "text/html; charset=utf-8"
    js = "text/javascript; charset=utf-8"
    css = "text/css; charset=utf-8"
    base = fixture_document(state, first_party_studio=True)
    test_script = b'<script type="module" src="/fixture/controller.mjs"></script>'
    styles = b'<link rel="stylesheet" href="/fixture/web_studio_shell.css">'
    result = {
        "/approved": (html, approved),
        "/ordinary": (html, fixture_document(state)),
        "/no-styles": (html, base.replace(b"</head>", test_script + b"</head>", 1)),
        "/composed": (
            html,
            base.replace(b"</head>", styles + test_script + b"</head>", 1),
        ),
        "/fixture/controller.mjs": (js, MODULE.encode()),
        web_sessions.STYLE: (css, (RUNTIME / "web_sessions.css").read_bytes()),
        web_sessions.SCRIPT: (js, (RUNTIME / "web_sessions.js").read_bytes()),
        **{
            f"/fixture/{name}.{ext}": (mime, (RUNTIME / f"{name}.{ext}").read_bytes())
            for name in (
                "web_studio_shell",
                "web_studio_transcript",
                "web_studio_composer",
            )
            for ext, mime in (("mjs", js), ("css", css))
        },
    }
    prefix = web_sessions.NAMED_STUDIO_ASSET_PREFIX
    named_script = (
        b'<script type="module" src="/fixture/named-controller.mjs"></script>'
    )
    named = fixture_document(named_studio=True)
    result.update(
        {
            "/named": (html, named.replace(b"</head>", named_script + b"</head>", 1)),
            "/named-inert": (html, named),
            "/fixture/named-controller.mjs": (js, NAMED_MODULE.encode()),
            prefix + "web_sessions.js": result[web_sessions.SCRIPT],
            prefix + "web_sessions.css": result[web_sessions.STYLE],
            **{
                prefix + name.rsplit("/", 1)[-1]: value
                for name, value in result.items()
                if name.startswith("/fixture/web_studio_")
            },
        }
    )
    for session in state["sessions"]:
        result[web_sessions.native_session_route(URL, session["id"])] = (
            html,
            b'<!doctype html><title>Fixture frame</title><textarea id="draft">Native fixture draft</textarea>',
        )
    return result


NAMED_PANES = [
    "#workspace-chat",
    "#workspace-library",
    "#workspace-divider-left",
    "#workspace-divider-right",
    "#workspace-overview",
]


def exercise_named(open_page, expect, approved, requests):
    start = len(requests)
    inert = open_page("/named-inert")
    expect(inert.locator("#no-selection")).to_have_text(
        "Studio controls are not connected."
    )
    expect(inert.locator("iframe, a, #session-controls")).to_have_count(0)
    expect(inert.locator(CENTER)).to_be_hidden()
    inert.get_by_role("button", name="Workspace manager").click()
    expect(inert.locator("#workspace-manager")).to_be_hidden()
    inert.close()

    page = open_page("/named")
    page.wait_for_function("typeof namedFixture !== 'undefined'")
    expect(page.locator("iframe, a, #session-controls")).to_have_count(0)
    expect(page.locator("#workspace-host-terminal")).to_be_visible()
    assert page.evaluate("namedFixture.bareMount() && namedFixture.noUntrustedMount()")
    page.evaluate("history.replaceState(null, '', '/named?session=ses_beta')")
    assert page.evaluate("namedFixture.attach()")
    assert page.evaluate("typeof ocBoxWebShell === 'undefined'")
    center = page.locator(CENTER)
    expect(center).to_be_hidden()
    assert page.evaluate("namedFixture.status().selected") is None
    assert page.evaluate("namedFixture.status().reads") == []
    initial_requests = len(requests)

    alpha = page.get_by_role("tab", name="Synthetic <alpha>")
    beta = page.get_by_role("tab", name="Synthetic Beta")
    alpha.evaluate("el => el.click()")
    assert page.evaluate("namedFixture.status().verifies") == []
    alpha.click()
    expect(center).to_be_hidden()
    page.evaluate("namedFixture.verify(true, 'ses_beta', true)")
    assert not page.evaluate("namedFixture.presented()")
    assert page.evaluate("namedFixture.status().reads") == []

    alpha.click()
    page.evaluate("namedFixture.verify(true, 'ses_alpha', true)")
    expect(center).to_be_hidden()
    page.evaluate("namedFixture.deliver('ses_alpha')")
    assert page.evaluate("namedFixture.presented()")
    expect(center.get_by_text("Named fixture text")).to_be_visible()
    expect(
        page.locator("#workspace-chat").get_by_role("heading", level=2)
    ).to_have_count(1)
    assert page.evaluate(GEOMETRY, NAMED_PANES) == approved
    expect(alpha).to_have_attribute("aria-selected", "true")
    field = center.get_by_role("textbox", name="Message", exact=True)
    send = center.get_by_role("button", name="Send", exact=True)
    field.fill("A named synthetic draft")

    page.get_by_role("button", name="Dashboard", exact=True).click()
    expect(page.locator("#workspace-overview")).to_be_visible()
    expect(field).to_have_value("A named synthetic draft")
    page.get_by_role("button", name="Chat focus").click()
    expect(page.locator("#workspace-library")).to_be_hidden()
    expect(field).to_have_value("A named synthetic draft")
    page.get_by_role("button", name="Studio", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 760})
    assert center.evaluate("""el => {
      const box = el.getBoundingClientRect();
      const compose = el.querySelector(".compose").getBoundingClientRect();
      return box.width === 390 && compose.width === 364 && compose.left === 13;
    }""")
    expect(page.locator("#workspace-host-terminal")).to_be_visible()
    page.locator("#workspace-sessions-toggle").click()
    assert page.locator("#workspace-chat").evaluate("el => el.inert")
    send.evaluate("el => el.click()")
    assert page.evaluate("namedFixture.status().sends") == []
    page.locator("#workspace-sessions-close").click()
    expect(field).to_have_value("A named synthetic draft")
    send.click()
    assert page.evaluate("namedFixture.status().sends") == [
        {"sessionID": "ses_alpha", "generation": 2, "text": "A named synthetic draft"},
    ]

    page.set_viewport_size(VIEWPORT)
    beta.click()
    expect(center).to_be_hidden()
    manager = page.get_by_role("button", name="Workspace manager")
    manager.click()
    expect(page.locator("#workspace-manager")).to_be_visible()
    assert page.evaluate("namedFixture.status().cancelled") == ["ses_beta"]
    page.evaluate("namedFixture.verify(true, 'ses_beta', true)")
    expect(center).to_be_hidden()
    expect(page.get_by_role("tab", name="Status")).to_be_focused()
    page.get_by_role("tab", name="Sessions").click()
    page.get_by_role("button", name="Refresh TUI tabs").click()
    rows = page.get_by_role("table", name="Workspace session results")
    expect(rows.get_by_role("row")).to_have_count(3)
    rows.get_by_role("button", name="Open in Studio: Synthetic Beta").click()
    expect(page.locator("#workspace-manager")).to_be_hidden()
    page.evaluate("namedFixture.verify(true, 'ses_beta', false)")
    page.evaluate("namedFixture.deliver('ses_beta')")
    assert page.evaluate("namedFixture.presented()")
    expect(center.get_by_text("Named fixture text")).to_be_visible()
    expect(send).to_have_count(0)
    expect(page.get_by_role("heading", name="Synthetic Beta")).to_be_visible()
    page.get_by_role("button", name="Workspace history").click()
    expect(page.locator("#workspace-search-status")).to_have_text(
        "Workspace history is not available in this browser."
    )
    page.get_by_role("button", name="TUI tabs").click()
    alpha.click()
    page.evaluate("namedFixture.verify(true, 'ses_alpha', true)")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    page.evaluate("namedFixture.deliver('ses_alpha')")
    assert not page.evaluate("namedFixture.presented()")
    expect(center).to_be_hidden()
    expect(page.locator("iframe, a, #session-controls")).to_have_count(0)
    assert page.evaluate("namedFixture.status().verifies") == [
        "ses_alpha",
        "ses_alpha",
        "ses_beta",
        "ses_beta",
        "ses_alpha",
    ]
    assert page.evaluate("namedFixture.status().catalogs") == [False]
    assert page.evaluate("namedFixture.status().selected") is None
    assert len(requests) == initial_requests
    assert all(
        path in ("/named", "/named-inert", "/fixture/named-controller.mjs")
        or path.startswith(web_sessions.NAMED_STUDIO_ASSET_PREFIX)
        for path in requests[start:]
    )


def exercise(browser, expect):
    allowed = assets()
    requests, unexpected, errors = [], [], []
    context = browser.new_context(
        viewport=VIEWPORT,
        color_scheme="dark",
        device_scale_factor=1,
        service_workers="block",
    )
    try:
        context.set_default_timeout(5000)

        def intercept(route):
            request = route.request
            path = urlsplit(request.url).path
            if (
                request.method != "GET"
                or request.url != ORIGIN + path
                or path not in allowed
            ):
                unexpected.append(request.url)
                route.abort()
            else:
                requests.append(path)
                content_type, body = allowed[path]
                route.fulfill(content_type=content_type, body=body)

        def deny_socket(ws):
            unexpected.append(ws.url)
            ws.close()

        def open_page(path):
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(error))
            page.goto(ORIGIN + path, wait_until="load")
            return page

        context.route("**/*", intercept)
        context.route_web_socket("**/*", deny_socket)
        design = open_page("/approved")
        approved = design.evaluate(
            GEOMETRY,
            [
                ".shell > main",
                ".library",
                '[data-split="left"]',
                '[data-split="right"]',
                ".inspector",
            ],
        )
        design.close()

        ordinary = open_page("/ordinary")
        expect(ordinary.locator(CENTER)).to_have_count(0)
        expect(ordinary.locator("#native")).to_be_visible()
        expect(
            ordinary.get_by_role("link", name="Open native OpenCode Home")
        ).to_be_visible()
        ordinary.close()

        no_styles = open_page("/no-styles")
        assert not no_styles.evaluate("fixture.attach('controller')")
        expect(no_styles.locator(CENTER)).to_be_hidden()
        expect(no_styles.locator("#native")).to_be_visible()
        no_styles.close()

        page = open_page("/composed")
        page.wait_for_function("fixture.nativeSelection() === 'ses_alpha'")
        center = page.locator(CENTER)
        native = page.locator("#native")
        frame_draft = page.frame_locator("#native").locator("#draft")
        expect(
            center.locator(":scope > #workspace-studio-transcript:empty")
        ).to_have_count(1)
        expect(
            center.locator(":scope > #workspace-studio-composer:empty")
        ).to_have_count(1)
        expect(center).to_be_hidden()
        expect(native).to_be_visible()
        assert not page.evaluate("fixture.attach('missing')")
        assert page.evaluate("fixture.attach('controller')")
        frame_draft.fill("Keep the original synthetic frame draft")
        original_reqs = len(requests)

        def grant(session="ses_alpha", writable=True):
            page.evaluate(
                "([session, writable]) => fixture.grant(session, writable)",
                [session, writable],
            )
            page.evaluate("fixture.start()")

        def deliver(valid=True):
            page.evaluate("valid => fixture.deliver(valid)", valid)
            return page.evaluate("fixture.presented()")

        grant()
        expect(native).to_be_visible()
        expect(center).to_be_hidden()
        assert not page.evaluate("fixture.probe()")
        assert len(page.evaluate("fixture.reads()")) == 1
        page.evaluate("fixture.invalidate()")
        assert not deliver()  # A late projection cannot reveal the center.
        expect(center).to_be_hidden()
        grant()
        assert not deliver(False)  # Wrong ID takes the unmodified fallback.
        expect(native).to_be_visible()
        page.evaluate("fixture.start()")
        assert not page.evaluate("fixture.presented()")
        assert (
            len(page.evaluate("fixture.reads()")) == 2
        )  # No retry of a consumed generation.

        grant(writable=False)
        assert deliver()
        expect(center).to_be_visible()
        expect(center.get_by_text("First-party fixture text")).to_be_visible()
        expect(center.get_by_role("button", name="Send")).to_have_count(0)
        page.evaluate("fixture.invalidate()")
        expect(native).to_be_visible()
        assert page.evaluate("fixture.nativeUnchanged()")
        expect(frame_draft).to_have_value("Keep the original synthetic frame draft")

        grant()
        assert deliver()
        expect(native).to_be_hidden()
        expect(
            page.locator("#workspace-chat").get_by_role("heading", level=2)
        ).to_have_count(1)
        expect(
            page.get_by_role("heading", name="Synthetic <alpha>", exact=True)
        ).to_be_visible()
        field = center.get_by_role("textbox", name="Message", exact=True)
        send = center.get_by_role("button", name="Send", exact=True)
        expect(send).to_be_disabled()
        assert page.evaluate("fixture.probe()")
        assert len(page.evaluate("fixture.reads()")) == 4
        measured = page.evaluate(
            GEOMETRY,
            [
                "#workspace-chat",
                "#workspace-library",
                "#workspace-divider-left",
                "#workspace-divider-right",
                "#workspace-overview",
            ],
        )
        assert measured == approved, (approved, measured)

        page.set_viewport_size({"width": 390, "height": 760})
        library, overview = (
            page.locator("#workspace-library"),
            page.locator("#workspace-overview"),
        )
        expect(library).to_be_hidden()
        expect(overview).to_be_hidden()
        assert center.evaluate("""el => {
          const box = el.getBoundingClientRect();
          const compose = el.querySelector(".compose").getBoundingClientRect();
          return box.width === 390 && compose.width === 364 && compose.left === 13;
        }""")
        page.locator("#workspace-sessions-toggle").click()
        expect(library).to_be_visible()
        assert page.locator("#workspace-chat").evaluate("el => el.inert")
        page.locator("#workspace-sessions-close").click()
        expect(library).to_be_hidden()
        expect(field).to_be_visible()

        field.fill("Queued only by native click fixture")
        send.evaluate("el => el.click()")
        assert page.evaluate("fixture.sends()") == []
        send.click()
        assert page.evaluate("fixture.sends()") == [
            {
                "sessionID": "ses_alpha",
                "generation": 4,
                "text": "Queued only by native click fixture",
            }
        ]
        page.locator("#workspace-sessions-toggle").click()
        expect(library).to_be_visible()
        assert page.locator("#workspace-chat").evaluate("el => el.inert")
        page.evaluate("fixture.settleSend('refused')")
        page.locator("#workspace-sessions-close").click()
        expect(field).to_have_value("Queued only by native click fixture")
        expect(center.get_by_role("status")).to_contain_text("Not sent")
        expect(send).to_be_enabled()
        field.fill("Late result after leaving Studio")
        send.click()
        assert page.evaluate("fixture.sends().at(-1)") == {
            "sessionID": "ses_alpha",
            "generation": 4,
            "text": "Late result after leaving Studio",
        }
        page.get_by_role("button", name="Workspace manager").click()
        expect(center).to_be_hidden()
        page.get_by_role("button", name="Studio", exact=True).click()
        expect(native).to_be_visible()
        assert page.evaluate("fixture.nativeUnchanged()")
        expect(frame_draft).to_have_value("Keep the original synthetic frame draft")
        page.evaluate("fixture.settleSend('unconfirmed')")
        expect(center).to_be_hidden()
        assert len(requests) == original_reqs

        grant()
        assert deliver()
        expect(field).to_have_value("")
        page.evaluate("fixture.clearWithoutAdvancing(); fixture.start()")
        assert not page.evaluate("fixture.presented()")
        expect(native).to_be_visible()
        # A shell event cannot authorize a read or a Send.
        page.evaluate("""window.dispatchEvent(new CustomEvent("oc-box:selection-changed", {
          detail: {sessionID:"ses_alpha", generation:999, writable:true}
        }))""")
        expect(center).to_be_hidden()
        assert len(page.evaluate("fixture.reads()")) == 5
        assert len(requests) == original_reqs

        page.set_viewport_size(VIEWPORT)
        page.get_by_role("tab", name="Synthetic Beta").click()
        page.wait_for_function("fixture.nativeSelection() === 'ses_beta'")
        grant("ses_alpha")
        assert not page.evaluate("fixture.presented()")
        expect(native).to_be_visible()
        grant("ses_beta")
        page.evaluate(
            "fixture.grant('ses_beta', true)"
        )  # Silent generation change during the read.
        assert not deliver()
        expect(native).to_be_visible()
        grant("ses_beta")
        assert deliver()
        expect(center).to_be_visible()
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        expect(center).to_be_hidden()
        expect(native).to_be_visible()
        assert requests[original_reqs:] == [
            web_sessions.native_session_route(URL, "ses_beta")
        ]
        exercise_named(open_page, expect, approved, requests)
        assert not unexpected and not errors
        return measured["relative"]
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
    expect.set_options(timeout=5000)
    with tempfile.TemporaryDirectory(
        prefix="oc-box-studio-shell-browser-"
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
                relative = exercise(browser, expect)
            finally:
                browser.close()
    print(
        f"PASS Chromium {version}: matched 2269x1398 shell tracks and composer {relative} CSS px."
    )
    print(
        "PASS 390px drawers, explicit handoff, bounded stale results, and unchanged native fallback."
    )
    print(
        "PASS iframe-free named Studio geometry at 2269x1398 and 390px, layouts, and trusted handoff; no native requests."
    )


if __name__ == "__main__":
    main()
