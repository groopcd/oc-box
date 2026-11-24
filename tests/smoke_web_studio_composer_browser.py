"""Offline Chromium smoke for the standalone Studio composer; no live shell.

Run from the repository root with a local Chromium or Chrome Headless Shell:
    uv run --offline --no-project --with playwright python -B \
      tests/smoke_web_studio_composer_browser.py --chromium /path/to/chromium

The approved mockup and synthetic fixture are fulfilled in the browser. No
server, live sessions, native POST, provider, credentials, or workspace state.
"""

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src/oc_box"
APPROVED_SHA256 = "cf044d2a71efc9220fc6b2be6c4e3651e44d6dfce88f70390fe8645cc5941af9"
ORIGIN = "https://studio-fixture.invalid"
VIEWPORT = {"width": 2269, "height": 1398}
MOUNT = "#workspace-studio-composer"
VIEW = """<!doctype html>
<html lang="en" data-layout="studio">
<meta charset="utf-8"><meta name="color-scheme" content="dark">
<link rel="stylesheet" href="/web_studio_transcript.css">
<link rel="stylesheet" href="/web_studio_composer.css">
<style>
body { margin: 0; background: #0a111b; }
#workspace-studio-center { position: absolute; }
#controls { position: fixed; top: 0; left: 0; }
#native { position: fixed; top: 0; right: 0; width: 190px; height: 74px; }
</style>
<div id="controls">
  <a href="#away">Leave conversation</a>
  <form action="/unexpected-form"><input name="outside" value="synthetic control"></form>
</div>
<main id="workspace-studio-center">
  <div id="workspace-studio-transcript"></div>
  <div id="workspace-studio-composer"></div>
</main>
<iframe id="native" srcdoc="<textarea id=draft>Separate fixture draft</textarea>"></iframe>
<script type="module" src="/fixture.mjs"></script>
"""

HARNESS = """
import { createStudioTranscriptView } from "/web_studio_transcript.mjs";
import { createStudioComposerView } from "/web_studio_composer.mjs";

const byId = id => document.getElementById(id);
const slot = byId("workspace-studio-composer");
const frame = byId("native");
const frameWindow = frame.contentWindow;
const sessionID = "ses_browser_fixture";
let generation = 1;
let selected = { sessionID, generation };
let settle = null, lastClick = null, former = null, submits = 0;
const gestures = [], calls = [];
document.addEventListener("submit", () => { submits++; }, true);
document.addEventListener("click", event => {
  if (!event.target.closest(".send")) return;
  lastClick = { trusted: event.isTrusted, detail: event.detail };
  if (gestures.length < 24) gestures.push(lastClick);
}, true);
const transcript = createStudioTranscriptView(byId("workspace-studio-transcript"), () => selected);
const composer = createStudioComposerView(slot, () => selected, action => {
  if (settle !== null || calls.length >= 8) throw new Error("Too many fixture calls");
  calls.push({ action, gesture: lastClick, frozen: Object.isFrozen(action) });
  return new Promise(resolve => { settle = resolve; });
});
function present() {
  composer.refresh();
  if (!selected) return;
  if (!transcript.commit(transcript.begin(), {
    sessionID, title: "Synthetic conversation", hasEarlier: false,
    messages: [{ id: "msg_browser_fixture", role: "user", text: "Read-only fixture", truncated: false }],
  })) throw new Error("Fixture projection refused");
}
window.addEventListener("hashchange", () => {
  former = slot.querySelector("textarea");
  composer.clear();
  transcript.clear();
  generation++;
  selected = location.hash ? null : { sessionID, generation };
  present();
});
window.fixture = {
  place: ({ left, top, width }) => {
    const center = byId("workspace-studio-center");
    Object.assign(center.style, { left: left + "px", top: top + "px", width: width + "px" });
    present();
  },
  calls: () => calls,
  gestures: () => gestures,
  resolve: result => {
    if (settle === null) throw new Error("No fixture call to resolve");
    const resolve = settle;
    settle = null;
    resolve(result);
  },
  generation: () => generation,
  replaceTranscript: present,
  invalidateWithoutNotification: () => { selected = { sessionID, generation: ++generation }; },
  previousFieldIsBlankAndDetached: () => former.value === "" && !former.isConnected,
  invariants: () => ({
    submits,
    formData: [...new FormData(document.querySelector("#controls form"))],
    storeCounts: [localStorage.length, sessionStorage.length],
    nativeUnchanged: frame === byId("native") && frame.contentWindow === frameWindow,
  }),
};
"""

MEASURE = """root => {
  const record = (selector, properties) => {
    const node = selector ? root.querySelector(selector) : root;
    const { left, top, right, width, height } = node.getBoundingClientRect();
    const css = getComputedStyle(node);
    return { left, top, right, width, height,
      styles: Object.fromEntries(properties.split(" ").filter(Boolean).map(key => [key, css[key]])) };
  };
  return {
    center: record(null, ""),
    header: record(".chat-head", "padding borderBottom backgroundColor display gap"),
    body: record(".chat-body", "width"),
    box: record(".compose", "padding border borderRadius backgroundColor"),
    field: record(".compose textarea", "font color minHeight padding border outline resize backgroundColor"),
    footer: record(".compose-foot", "display alignItems flexWrap gap"),
    send: record(".compose-foot .send", "padding border borderRadius color backgroundColor fontWeight opacity"),
    icon: record(".compose-foot .send .icon", "width height verticalAlign fill stroke strokeWidth strokeLinecap strokeLinejoin"),
    note: record(".composition-note", "font color marginTop"),
  };
}"""


def assert_geometry(approved, rendered):
    # This is the center at the mockup's actual desktop position and width.
    # There is no full-shell or screenshot comparison. Ignore the transcript
    # area's own height now that the unchanged transcript and composer have
    # sibling slots. The omitted demo tags and Send label affect button width.
    comparable_edges = {
        "center": ("left", "top", "width", "height"),
        "header": ("left", "top", "width", "height"),
        "body": ("left", "top", "width"),
        "box": ("left", "top", "width", "height"),
        "field": ("left", "top", "width", "height"),
        "footer": ("left", "top", "width", "height"),
        "send": ("top", "right", "height"),
        "icon": ("top", "width", "height"),
        "note": ("left", "top", "width", "height"),
    }
    for key, edges in comparable_edges.items():
        for edge in edges:
            want, got = approved[key][edge], rendered[key][edge]
            assert want == got, (key, edge, want, got)
        assert approved[key]["styles"] == rendered[key]["styles"], (
            key,
            approved[key]["styles"],
            rendered[key]["styles"],
        )


def activate(page, expect):
    slot = page.locator(MOUNT)
    field = slot.get_by_role("textbox", name="Message", exact=True)
    send = slot.get_by_role("button", name="Send", exact=True)
    status = slot.get_by_role("status", include_hidden=True)

    def click_disabled():
        expect(send).to_be_disabled()
        box = send.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    expect(
        slot.locator(":scope > .compose > textarea + .compose-foot > .send")
    ).to_have_count(1)
    expect(slot.locator(":scope > .compose + .composition-note")).to_have_count(1)
    expect(slot.locator("form, input, select, a, .tag, iframe")).to_have_count(0)
    assert slot.evaluate("""slot => [...slot.querySelectorAll("textarea,button")].every(
      node => node.form === null && !node.hasAttribute("name"))""")
    expect(send).to_have_attribute("type", "button")
    expect(send.locator("svg path")).to_have_attribute("d", "M12 20V4m-6 6 6-6 6 6")
    expect(send).to_be_disabled()
    field.press_sequentially("Pointer")
    page.keyboard.press("Enter")
    field.press_sequentially("line")
    expect(field).to_have_value("Pointer\nline")
    expect(send).to_be_enabled()
    send.evaluate("""node => {
      node.click();
      node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      node.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    }""")
    assert page.evaluate("fixture.gestures()") == [{"trusted": False, "detail": 0}] * 2
    assert page.evaluate("fixture.calls()") == []
    field.evaluate("""node => {
      node.value = "Programmatic replacement";
      node.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }""")
    send.click()  # A real click still cannot send text that the draft does not own.
    assert page.evaluate("fixture.calls()") == []
    field.fill("Pointer\nline")
    page.evaluate("fixture.replaceTranscript()")
    expect(field).to_have_value("Pointer\nline")
    send.click()
    expect(status).to_contain_text("Sending to native OpenCode")
    click_disabled()
    assert page.evaluate("fixture.calls()") == [
        {
            "action": {
                "sessionID": "ses_browser_fixture",
                "generation": 1,
                "text": "Pointer\nline",
            },
            "gesture": {"trusted": True, "detail": 1},
            "frozen": True,
        }
    ]
    field.fill("Edited while sending")
    field.fill("Pointer\nline")
    page.evaluate("fixture.resolve('queued')")
    expect(status).to_contain_text("Execution is not confirmed")
    expect(field).to_have_value("Pointer\nline")  # Same bytes, newer revision.

    field.fill("Keyboard test")
    field.focus()
    page.keyboard.press("Tab")
    expect(send).to_be_focused()
    page.keyboard.press("Space")
    assert page.evaluate("fixture.calls().at(-1)") == {
        "action": {
            "sessionID": "ses_browser_fixture",
            "generation": 1,
            "text": "Keyboard test",
        },
        "gesture": {"trusted": True, "detail": 0},
        "frozen": True,
    }
    page.evaluate("fixture.resolve('unconfirmed')")
    expect(status).to_contain_text("The message may already be queued")
    expect(field).to_have_value("Keyboard test")
    field.fill("Edited after uncertainty")
    expect(status).to_contain_text("Check this conversation in native OpenCode")
    click_disabled()
    assert page.evaluate("fixture.calls().length") == 2
    assert page.url == ORIGIN + "/view"

    def away_and_back(generation):
        page.get_by_role("link", name="Leave conversation").click()
        expect(slot).to_be_hidden()
        page.go_back()
        expect(field).to_be_visible()
        expect(field).to_have_value("")
        assert page.evaluate("fixture.generation()") == generation
        assert page.evaluate("fixture.previousFieldIsBlankAndDetached()")

    away_and_back(3)
    field.fill("Old selection")
    send.click()
    expect(send).to_be_disabled()
    away_and_back(5)
    field.fill("New selection")
    expect(send).to_be_disabled()
    page.evaluate("fixture.resolve('queued')")
    expect(send).to_be_enabled()
    expect(field).to_have_value("New selection")
    expect(status).to_be_empty()
    assert page.evaluate("fixture.calls().length") == 3

    send.click()
    page.evaluate("fixture.resolve('refused')")
    expect(status).to_contain_text("Not sent")
    expect(field).to_have_value("New selection")
    expect(send).to_be_enabled()

    send.click()
    page.evaluate("fixture.resolve('queued')")
    expect(status).to_contain_text("acknowledged")
    # A known receipt clears only the submitted revision.
    expect(field).to_have_value("")
    expect(send).to_be_disabled()

    field.fill("Became stale after Send")
    send.click()
    page.evaluate("fixture.invalidateWithoutNotification()")
    page.evaluate("fixture.resolve('unconfirmed')")
    expect(slot).to_be_hidden()
    expect(status).to_be_empty()
    page.evaluate("fixture.replaceTranscript()")
    expect(field).to_have_value("")

    field.fill("Became stale before Send")
    page.evaluate("fixture.invalidateWithoutNotification()")
    send.click()
    expect(slot).to_be_hidden()
    expect(slot.locator("textarea")).to_have_value("")
    assert page.evaluate("fixture.calls().map(call => call.action.generation)") == [
        1,
        1,
        3,
        5,
        5,
        5,
    ]

    assert page.evaluate("fixture.invariants()") == {
        "submits": 0,
        "formData": [["outside", "synthetic control"]],
        "storeCounts": [0, 0],
        "nativeUnchanged": True,
    }
    expect(page.locator("iframe")).to_have_count(1)
    expect(page.frame_locator("#native").locator("#draft")).to_have_value(
        "Separate fixture draft"
    )


def exercise(browser, expect):
    approved = (ROOT / "docs/mockups/web-manager.html").read_bytes()
    assert hashlib.sha256(approved).hexdigest() == APPROVED_SHA256, (
        "Approved mockup changed"
    )
    assets = {
        "/approved": ("text/html", approved),
        "/view": ("text/html", VIEW.encode()),
        "/fixture.mjs": ("text/javascript", HARNESS.encode()),
        **{
            f"/web_studio_{view}.{ext}": (
                mime,
                (RUNTIME / f"web_studio_{view}.{ext}").read_bytes(),
            )
            for view in ("composer", "transcript")
            for ext, mime in (("mjs", "text/javascript"), ("css", "text/css"))
        },
    }
    routes = {ORIGIN + path: body for path, body in assets.items()}
    context = browser.new_context(
        viewport=VIEWPORT,
        color_scheme="dark",
        device_scale_factor=1,
        service_workers="block",
    )
    try:
        context.set_default_timeout(5000)
        requests, sockets, errors = [], [], []

        def serve(route):
            request = route.request
            requests.append((request.method, request.url))
            asset = routes.get(request.url)
            if request.method != "GET" or asset is None:
                route.abort()
            else:
                mime, body = asset
                route.fulfill(content_type=mime, body=body)

        def deny_socket(ws):
            sockets.append(ws.url)
            ws.close()

        def page_at(path):
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(error))
            page.goto(ORIGIN + path, wait_until="load")
            return page

        context.route("**/*", serve)
        context.route_web_socket("**/*", deny_socket)
        design = page_at("/approved")
        center = design.locator(".shell > main > section[aria-labelledby=chat-title]")
        expect(center.locator(".compose")).to_be_visible()
        reference = center.evaluate(MEASURE)
        design.close()

        page = page_at("/view")
        page.wait_for_function("window.fixture !== undefined")
        page.evaluate("frame => fixture.place(frame)", reference["center"])
        slot = page.locator(MOUNT)
        expect(slot).to_be_visible()
        # Compare enabled styling; the real disabled cue is intentional.
        field = slot.get_by_role("textbox", name="Message", exact=True)
        field.fill("Synthetic geometry draft")
        expect(slot.get_by_role("button", name="Send", exact=True)).to_be_enabled()
        field.blur()
        measurements = page.locator("#workspace-studio-center").evaluate(MEASURE)
        assert_geometry(reference, measurements)
        field.fill("")
        assert sorted(requests) == sorted(("GET", url) for url in routes)
        start = len(requests)
        activate(page, expect)
        assert len(requests) == start and not sockets and not errors
        return measurements["box"]
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
        prefix="oc-box-studio-composer-browser-"
    ) as directory:
        root = Path(directory)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root),
            "TMPDIR": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
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
                box = exercise(browser, expect)
            finally:
                browser.close()
    rect = ", ".join(str(box[key]) for key in ("left", "top", "width", "height"))
    print(
        f"PASS Chromium {version} at 2269x1398: approved composer box ({rect}) CSS px."
    )
    print(
        "PASS trusted pointer/keyboard, exact draft and selection races, and receipt states."
    )
    print(
        "PASS no submission, HTTP/WebSocket request after bootstrap, or native fixture mutation."
    )


if __name__ == "__main__":
    main()
