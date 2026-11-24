"""Offline Chromium check of the source-only Studio transcript component.

Run with the local browser executable and optional Playwright:
    uv run --no-project --with playwright python -B \
      tests/smoke_web_studio_transcript_browser.py --chromium /path/to/chromium

Synthetic projection and sibling iframe only; no server or native workspace.
"""

import argparse
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src/oc_box"
MOUNT = "#workspace-studio-transcript"
PROJECTION = {
    "sessionID": "ses_view_fixture",
    "title": "<b>Synthetic conversation</b>",
    "hasEarlier": True,
    "messages": [
        {
            "id": "msg_fixture_user",
            "role": "user",
            "text": "First line\n  "
            + "<img src=https://example.invalid/blocked onerror=window.ran=1>"
            + "x" * 250,
            "truncated": False,
        },
        {
            "id": "msg_fixture_answer",
            "role": "assistant",
            "text": "<script>window.ran=1</script>\nhttps://example.invalid/",
            "truncated": True,
        },
    ],
}


def exercise(browser, expect):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.set_default_timeout(5000)
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(error))
        context.route("**/*", lambda route: route.abort())
        page.set_content("""<!doctype html><html lang="en"><body style="margin:0">
          <main id="first-party" data-layout="studio">
            <nav id="existing">Controls outside the transcript</nav>
            <div id="workspace-studio-transcript"></div>
            <iframe id="native" srcdoc="<textarea id=draft>Keep this draft</textarea>"></iframe>
          </main></body></html>""")
        page.add_style_tag(path=RUNTIME / "web_studio_transcript.css")
        page.add_script_tag(
            type="module",
            content=(RUNTIME / "web_studio_transcript.mjs").read_text(encoding="utf-8")
            + """
            const mount = document.querySelector("#workspace-studio-transcript");
            let selected = {sessionID: "ses_view_fixture", generation: 1};
            const view = createStudioTranscriptView(mount, () => selected);
            const frame = document.querySelector("#native");
            const original = frame.contentWindow;
            window.fixture = {
              render: value => view.commit(view.begin(), value),
              capture: () => { window.ticket = view.begin(); },
              advance: () => { selected = {...selected, generation: selected.generation + 1}; },
              late: value => view.commit(window.ticket, value),
              unchanged: () => frame === document.querySelector("#native") &&
                frame.contentWindow === original && !frame.hasAttribute("src"),
            };
            """,
        )
        page.wait_for_function("window.fixture !== undefined")
        frame = page.frame_locator("#native")
        expect(frame.locator("#draft")).to_have_value("Keep this draft")
        assert page.evaluate("value => fixture.render(value)", PROJECTION)
        slot = page.locator(MOUNT)
        expect(slot.get_by_role("heading", level=2)).to_have_text(PROJECTION["title"])
        expect(slot.get_by_role("list", name="Conversation messages")).to_be_visible()
        expect(slot.get_by_role("listitem")).to_have_count(2)
        assert slot.locator(".by strong").all_text_contents() == ["You", "OpenCode"]
        assert slot.locator(".message p").all_text_contents() == [
            row["text"] for row in PROJECTION["messages"]
        ]
        expect(slot.locator(".avatar[aria-hidden=true]")).to_have_count(2)
        expect(slot.locator(".transcript-earlier")).to_contain_text("may not be shown")
        expect(slot.locator(".transcript-clipped")).to_have_count(1)
        assert (
            slot.locator(
                "a, img, form, button, input, textarea, iframe, script"
            ).count()
            == 0
        )
        assert page.evaluate("window.ran === undefined")
        assert slot.evaluate("""slot => {
          const body = slot.querySelector('.chat-body');
          const box = body.getBoundingClientRect();
          const css = getComputedStyle(body), by = getComputedStyle(slot.querySelector('.by'));
          const user = getComputedStyle(slot.querySelector('.message.you p'));
          const avatar = getComputedStyle(slot.querySelector('.avatar'));
          const heading = getComputedStyle(slot.querySelector('h2'));
          return box.width === 760 && box.left === (slot.clientWidth - 760) / 2 &&
            css.padding === '22px 24px 16px' && css.fontSize === '14px' &&
            css.lineHeight === '21px' && heading.fontSize === '18px' &&
            by.fontSize === '12px' && by.gap === '8px' &&
            avatar.width === '24px' && avatar.height === '24px' &&
            user.whiteSpace === 'pre-wrap' && user.overflowWrap === 'anywhere' &&
            user.padding === '11px 13px' && user.backgroundColor === 'rgb(18, 33, 49)';
        }""")
        for layout, height in (("dashboard", 526), ("focus", 700)):
            page.locator("#first-party").evaluate(
                "(main, layout) => main.dataset.layout = layout", layout
            )
            assert slot.locator(".chat-body").evaluate(
                "(el, height) => el.getBoundingClientRect().height === height", height
            )
        assert slot.locator(".chat-head").evaluate(
            "el => getComputedStyle(el).paddingInlineStart === '283px'"
        )

        page.locator("#first-party").evaluate("main => main.dataset.layout = 'studio'")
        page.set_viewport_size({"width": 390, "height": 700})
        assert slot.evaluate("""slot => {
          const body = slot.querySelector('.chat-body');
          const text = slot.querySelector('.message.you p');
          return body.getBoundingClientRect().width === slot.clientWidth &&
            getComputedStyle(body).padding === '14px 13px' && text.scrollWidth <= text.clientWidth;
        }""")

        page.evaluate("fixture.capture()")
        page.evaluate("fixture.advance()")
        assert not page.evaluate("value => fixture.late(value)", PROJECTION)
        expect(slot.locator(".message")).to_have_count(0)
        assert page.evaluate("fixture.unchanged()")
        expect(page.locator("iframe")).to_have_count(1)
        expect(frame.locator("#draft")).to_have_value("Keep this draft")
        expect(page.locator("#existing")).to_have_text(
            "Controls outside the transcript"
        )
        assert not requests and not errors
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path, required=True)
    options = parser.parse_args()
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        parser.error("Playwright is required; see --help")
    chromium = options.chromium.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="oc-box-studio-view-") as directory:
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
                args=["--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND"],
            )
            try:
                exercise(browser, expect)
            finally:
                browser.close()
    print("PASS offline Chromium: read-only Studio transcript styling and rendering.")


if __name__ == "__main__":
    main()
