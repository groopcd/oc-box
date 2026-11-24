"""Provider-free Chromium smoke for the real workspace File attachment adapter.

Run from the repository root with a tested, unmodified native binary:

  uv run --no-project --with playwright python -B tests/smoke_web_attachments_browser.py \
    --opencode /path/to/opencode-v2 --chromium /path/to/chromium

The real web launch, pairing, shell and native composer run over loopback.
Only Podman exec/relay is replaced by local processes. Private stores and
browser contexts are disposable; nothing is sent to a provider. The file
chooser and DevTools drag produce real trusted browser events, not physical
desktop drag/portal input or rootless-Podman acceptance. Use a short TMPDIR
if Chromium reports its Unix socket path is too long.
"""

import argparse
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from smoke_native_tabs_v2 import Client
from smoke_session_sync_v2 import bounded, private_environment, require, seed
from smoke_web_attachments_v2 import PNG, pdf
from smoke_web_layout_browser import executable, launch_browser, rendered_session

from oc_box import native_tabs, opencode_compat, web_attachments
from oc_box import oc_box as box
from oc_box import opencode_v2_io as bridge
from oc_box import workspace_service as service

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


ALPHA = "ses_attachment_alpha"
BRAVO = "ses_attachment_bravo"
EDITOR = '[contenteditable="true"]'
CHOOSER = "Choose image or PDF attachments"
PNG_NAME = "synthetic-browser-only.png"
PDF_NAME = "synthetic-native-drop.pdf"
DRAFT_TEXT = "SYNTHETIC_KEEP_EXISTING_DRAFT"


def composer(page, gateway, ident, title):
    rendered_session(page, gateway, ident, title)
    expect(page.get_by_label(CHOOSER)).to_be_enabled()
    require(
        page.evaluate(
            "() => document.querySelector('#native').contentWindow.location.pathname"
        )
        == gateway.native_session_route(ident),
        "The live native iframe route did not match the selected ID.",
    )
    editor = page.frame_locator("#native").locator(EDITOR)
    expect(editor).to_have_count(1)
    expect(editor).to_be_editable()
    return editor


def native_drop(page, file, editor):
    editor.scroll_into_view_if_needed()
    bounds = editor.bounding_box()
    require(bounds is not None, "No native composer drop coordinates.")
    session = page.context.new_cdp_session(page)
    try:
        for event in ("dragEnter", "dragOver", "drop"):
            session.send(
                "Input.dispatchDragEvent",
                {
                    "type": event,
                    "x": bounds["x"] + bounds["width"] / 2,
                    "y": bounds["y"] + bounds["height"] / 2,
                    "data": {
                        "items": [
                            {
                                "mimeType": "text/plain",
                                "data": "file:///synthetic/do-not-forward.pdf",
                            }
                        ],
                        "files": [str(file)],
                        "dragOperationsMask": 1,
                    },
                },
            )
    finally:
        session.detach()
    page.wait_for_function(
        """() => document.querySelector('#native').contentWindow.__attachmentProbe.forwarded"""
    )


def check_drafts(page, gateway, root, native):
    def snapshot():
        return {
            ident: (
                native("GET", "/api/experimental/session/" + ident + "/export"),
                native("GET", "/api/session/" + ident + "/inbox"),
            )
            for ident in (item["id"] for item in bridge.service_session_catalog(native))
        }

    before = snapshot()
    require(set(before) == {ALPHA, BRAVO}, "Unexpected private session inventory.")
    admitted = []

    def record_request(request):
        path = urlsplit(request.url).path.split("/")
        if request.method not in ("GET", "HEAD", "OPTIONS") and any(
            part in ("prompt", "inbox", "resume") for part in path
        ):
            admitted.append(True)

    page.context.on("request", record_request)
    native_frame = page.frame_locator("#native")
    png = native_frame.locator(f'img[alt="{PNG_NAME}"]')
    card = native_frame.locator('[data-slot="attachment-card-title"]').filter(
        has_text=PDF_NAME
    )
    pdf_status = card.locator("..").locator('[data-slot="attachment-card-subtitle"]')
    status = page.locator("#addon-status")
    chooser = page.get_by_label(CHOOSER)
    alpha = composer(page, gateway, ALPHA, "Synthetic Alpha")
    alpha.fill(DRAFT_TEXT)
    page.evaluate(
        """() => {
          const w = document.querySelector('#native').contentWindow;
          const proof = w.__attachmentProbe = { chosen: false, forwarded: false };
          window.addEventListener('change', event => {
            if (event.target.matches?.('input[type=file]')) {
              proof.chosen ||= event.isTrusted && event.target.files.length === 1;
            }
          }, true);
          let original = null;
          w.addEventListener('drop', event => {
            const types = [...event.dataTransfer.types];
            if (event.isTrusted && event.target.closest?.('[contenteditable=true]')
                && event.dataTransfer.files.length === 1 && types.includes('text/plain')) {
              original = event.target;
            }
            if (!event.isTrusted && original && event.target === original &&
                event.dataTransfer.files.length === 1 &&
                types.length === 1 && types[0] === 'Files') {
              proof.forwarded = true;
            }
          }, true);
        }"""
    )

    png_file = root / PNG_NAME
    png_file.write_bytes(base64.b64decode(PNG))
    pdf_file = root / PDF_NAME
    pdf_file.write_bytes(pdf())
    page.get_by_text("Browser attachments", exact=True).click()
    expect(chooser).to_be_visible()
    with page.expect_file_chooser() as dialog:
        chooser.click()
    # A real file path here uses Chromium's chooser, unlike an in-memory
    # Playwright payload that synthesizes an untrusted change event.
    dialog.value.set_files(png_file)
    require(
        page.evaluate(
            "() => document.querySelector('#native').contentWindow.__attachmentProbe.chosen"
        ),
        "The chooser did not deliver a trusted browser event.",
    )
    expect(status).to_contain_text("Forwarded 1 file(s)")
    expect(png).to_be_visible()
    expect(png).to_have_js_property("naturalWidth", 1)
    expect(alpha).to_have_text(DRAFT_TEXT)
    native_drop(page, pdf_file, alpha)
    expect(status).to_contain_text("Forwarded 1 file(s)")
    expect(card).to_have_text(PDF_NAME)
    # Native first renders a provisional card with upload progress. Switching
    # sessions before native finishes staging can discard that pending file.
    # This is a test oracle for the unmodified UI, not an adapter widget hook.
    expect(pdf_status).to_have_text("PDF", timeout=15_000)
    expect(png).to_be_visible()
    expect(alpha).to_have_text(DRAFT_TEXT)

    page.get_by_role("tab", name="Synthetic Bravo", exact=True).click()
    bravo = composer(page, gateway, BRAVO, "Synthetic Bravo")
    expect(bravo).to_have_text("")
    expect(png).to_have_count(0)
    expect(card).to_have_count(0)
    page.get_by_role("tab", name="Synthetic Alpha", exact=True).click()
    alpha = composer(page, gateway, ALPHA, "Synthetic Alpha")
    expect(alpha).to_have_text(DRAFT_TEXT)
    expect(png).to_be_visible()
    expect(pdf_status).to_have_text("PDF")

    require(not admitted, "The browser issued a prompt, inbox or resume mutation.")
    require(
        snapshot() == before and native("GET", "/api/session/active")["data"] == {},
        "Native history, inbox, session inventory or activity changed.",
    )


def exercise(binary, browser, root):
    project = root / "synthetic-project"
    project.mkdir()
    env = private_environment(root / "native")
    home = Path(env["HOME"])
    for kind, relative in (
        ("CONFIG", ".config"),
        ("STATE", ".local/state"),
        ("DATA", ".local/share"),
        ("CACHE", ".cache"),
    ):
        env["XDG_" + kind + "_HOME"] = str(home / relative)
    env.update(
        TERM="xterm-256color", COLORTERM="truecolor", OPENCODE_DISABLE_AUTOUPDATE="1"
    )
    config = home / ".config/opencode"
    config.mkdir(parents=True)
    (config / "cli.json").write_text(
        '{"tabs":{"mode":"on","scope":"cwd","layout":"horizontal"},'
        '"animations":false,"highlights":false}',
        encoding="utf-8",
    )
    version = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(
        web_attachments.supports_release(version), web_attachments.UNVERIFIED_RELEASE
    )
    data = {
        "id": "a" * 32,
        "project": str(project),
        "home": str(home),
        "container": "synthetic-no-podman",
        "opencode_version": version,
    }
    original_run = box.run

    def local_exec(command, **settings):
        marker = command.index(data["container"])
        require(
            isinstance(command, box.ContainerCommand)
            and command[:3] == ["podman", "exec", "-i"]
            and command[marker + 1 : marker + 5] == ["python3", "-I", "-S", "-c"],
            "The fixture lost the private production command boundary.",
        )
        if "input" not in settings:
            settings["stdin"] = subprocess.DEVNULL
        return original_run(
            command[marker + 1 :], cwd=project, env=command.environment, **settings
        )

    def local_relay(_box, _data, port):
        require(port == endpoint.port, "Unexpected private relay endpoint.")
        return box.ContainerCommand(
            [sys.executable, "-B", "-c", service.PROXY, str(port)], env
        )

    previous = Path.cwd()
    os.chdir(project)
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            bridge.server(binary, startup_deadline=time.monotonic() + 35) as native,
        ):
            endpoint = urlsplit(native.endpoint["url"])
            require(
                native("GET", "/api/info")["version"] == version
                and endpoint.hostname == "127.0.0.1",
                "Private native service identity mismatch.",
            )
            require(
                native("GET", "/api/credential")["data"] == [],
                "The fixture inherited native credentials.",
            )
            seed(native, ALPHA, project, title="Synthetic Alpha")
            seed(native, BRAVO, project, title="Synthetic Bravo")
            for expected in ((ALPHA,), (ALPHA, BRAVO)):
                client = Client(binary, env, project, native.endpoint, expected[-1])
                try:
                    client.wait(
                        lambda: (
                            native_tabs.read_snapshot(
                                str(home), str(project), version
                            ).tabs
                            == expected
                        ),
                        "browser attachment native membership",
                    )
                    client.drain(1.3)
                finally:
                    client.close()
            connection = {
                **native.endpoint,
                "workspace": data["id"],
                "session": ALPHA,
                "version": version,
            }
            with (
                patch.object(box, "CONTAINER_OPENCODE", binary),
                patch.object(box, "run", side_effect=local_exec),
                patch.object(service, "relay_command", side_effect=local_relay),
            ):
                launch_browser(
                    browser,
                    data,
                    connection,
                    ["--attachments", "--session", ALPHA],
                    lambda page, gateway: check_drafts(page, gateway, root, native),
                )
    finally:
        os.chdir(previous)
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=executable)
    parser.add_argument("--chromium", required=True, type=executable)
    args = parser.parse_args()
    if sync_playwright is None:
        parser.error("Playwright is required; see the invocation in --help")
    with tempfile.TemporaryDirectory(prefix="att-") as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in private_environment(root / "browser").items()
            if not key.startswith("OPENCODE_")
        }
        with patch.dict(os.environ, env, clear=True), sync_playwright() as driver:
            browser = driver.chromium.launch(
                executable_path=args.chromium,
                headless=True,
                env=env,
                timeout=15_000,
                args=[
                    "--no-proxy-server",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                ],
            )
            try:
                version = exercise(args.opencode, browser, root)
                print(
                    f"PASS Chromium {browser.version}, native {version}: trusted PNG "
                    "chooser and PDF composer drop, file-only same-target forwarding, "
                    "two isolated drafts, and no prompt admission or execution."
                )
            finally:
                browser.close()
    print(
        "Offline synthetic Chromium only; NOT desktop drag or host Podman acceptance."
    )


if __name__ == "__main__":
    main()
