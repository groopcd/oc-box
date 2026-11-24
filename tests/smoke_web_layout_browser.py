r"""Offline Chromium smoke test for the oc-box workspace web launch.

Run from the repository root with an unmodified stable OpenCode V2 executable
and an installed Chromium (or Chrome Headless Shell) executable:

  uv run --no-project --with playwright python -B tests/smoke_web_layout_browser.py \
    --opencode /path/to/opencode-v2 --chromium /path/to/chromium

Uses disposable browser contexts, a stdio-owned native service and synthetic
TUI tabs. Only Podman exec is replaced with local execution. No shared native
service, host workspace, provider, or live Podman acceptance. HTTP loopback only;
this does not test TLS, remote access, or an installed browser profile.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from smoke_native_tabs_v2 import Client
from smoke_session_sync_v2 import (
    FixtureFailure,
    bounded,
    exported,
    private_environment,
    require,
    seed,
)

from oc_box import native_tabs, opencode_compat, web_history, web_sessions
from oc_box import oc_box as box
from oc_box import opencode_v2_io as bridge
from oc_box import workspace_service as service

try:
    from playwright.sync_api import Error, expect, sync_playwright
except ImportError:
    sync_playwright = None


@contextmanager
def browser_page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        context.set_default_timeout(15_000)
        yield context.new_page()
    finally:
        context.close()


def pair(browser, page, link, gateway):
    index = gateway.url + web_sessions.INDEX
    anonymous = page.goto(index, wait_until="domcontentloaded")
    require(
        anonymous is not None and anonymous.status == 401,
        "Anonymous shell did not refuse.",
    )

    def follow(target):
        try:
            return target.goto(link, wait_until="domcontentloaded")
        except Error:
            # Playwright's raw navigation error can contain the pairing link.
            raise FixtureFailure("Browser pairing navigation failed.") from None

    reply = follow(page)
    require(
        reply is not None
        and reply.status == 200
        and reply.url == index
        and reply.request.redirected_from is not None
        and reply.request.redirected_from.url == link,
        "One-use native pairing did not redirect to the authenticated shell.",
    )
    with browser_page(browser) as fresh:
        replay = follow(fresh)
        require(
            replay is not None and replay.url != index,
            "Pairing link replay reached the shell.",
        )
        anonymous = fresh.goto(index, wait_until="domcontentloaded")
        require(
            anonymous is not None and anonymous.status == 401,
            "Pairing replay authenticated a fresh context.",
        )
    # Replay used a different top-level page. Restore the paired page's input
    # focus before driving real controls in it.
    page.bring_to_front()


def launch_browser(browser, data, connection, flags, inspect):
    """Follow the link printed by the real web launcher before its health loop."""
    args = service.web_parser().parse_args(["--no-open", *flags])
    args.web_session = args.session
    gateway_type, gateway = service.Gateway, None
    output = io.StringIO()

    def remember_gateway(*args, **kwargs):
        nonlocal gateway
        gateway = gateway_type(*args, **kwargs)
        return gateway

    def before_keepalive(*_):
        lines = output.getvalue().splitlines()
        require(
            gateway is not None
            and len(lines) == 3
            and lines[1].startswith(gateway.url + "/auth/connect/"),
            "The launcher did not print a one-use link for its private gateway.",
        )
        with browser_page(browser) as page:
            pair(browser, page, lines[1], gateway)
            inspect(page, gateway)
        raise KeyboardInterrupt

    with (
        patch.object(service, "Gateway", side_effect=remember_gateway),
        patch.object(service, "build_opener", side_effect=before_keepalive) as opened,
        redirect_stdout(output),
    ):
        result = service.web_session(box, data, connection, args)
    require(result == 0 and opened.call_count == 1, "Web launch did not finish.")


def rendered_session(page, gateway, ident, title):
    """Observe the real native app, not an iframe with only a plausible src."""
    frame = page.locator("iframe")
    expect(frame).to_have_count(1)
    expect(frame).to_have_attribute("id", "native")
    expect(frame).to_have_attribute("src", gateway.native_session_route(ident))
    expect(frame).to_be_visible()
    native = page.frame_locator("#native")
    expect(native.get_by_role("heading", name=title, exact=True)).to_be_visible(
        timeout=15_000
    )
    # Native renders this span using display: contents (no box of its own).
    expect(native.get_by_text("SYNTHETIC_" + ident, exact=True)).to_be_attached(
        timeout=15_000
    )


def manager_views(page, preserved):
    manager = page.locator("#workspace-manager")
    sections = page.get_by_role("tablist", name="Workspace manager sections")
    status = sections.get_by_role("tab", name="Status", exact=True)
    backup = sections.get_by_role("tab", name="Backup/restore", exact=True)
    settings_tab = sections.get_by_role("tab", name="Settings", exact=True)
    selected_panel = manager.get_by_role("tabpanel")
    expect(sections.get_by_role("tab")).to_have_count(7)
    expect(manager).to_contain_text("This browser view is limited to this workspace")
    expect(selected_panel).to_have_count(1)
    expect(status).to_be_focused()
    expect(page.locator("#workspace-manager-tab-count")).to_have_text("2")
    expect(page.locator("#workspace-manager-selection")).to_have_text("Synthetic Alpha")
    expect(page.locator("#workspace-manager-history-status")).to_have_text(
        "Not requested"
    )
    require(
        manager.evaluate(
            """el => {
              const style = node => getComputedStyle(node);
              const strip = el.querySelector('[role=tablist]');
              const tab = strip.querySelector('[aria-selected=true]');
              return style(el).padding === "22px" && style(strip).gap === "4px" &&
                style(tab).boxShadow === "rgb(120, 224, 230) 0px -2px 0px 0px inset" &&
                style(el.querySelector('.workspace-manager-card')).backgroundColor === "rgb(17, 30, 43)";
            }"""
        ),
        "The approved manager's tabs or cards did not render.",
    )

    def active(tab, panel_name):
        expect(tab).to_be_focused()
        expect(tab).to_have_attribute("aria-selected", "true")
        expect(tab).to_have_attribute("tabindex", "0")
        expect(selected_panel).to_have_count(1)
        expect(selected_panel).to_have_accessible_name(panel_name)

    status.press("ArrowLeft")
    active(backup, "Backup/restore")
    backup.press("ArrowRight")
    active(status, "Status")
    status.press("End")
    active(backup, "Backup/restore")
    backup.press("Home")
    active(status, "Status")
    for key, name, guidance in (
        ("4", "Terminals", "oc-box terminal"),
        ("5", "Logs", "oc-box logs"),
        ("6", "Storage", "oc-box tui"),
        ("7", "Backup/restore", "oc-box restore"),
    ):
        sections.locator("[aria-selected=true]").press(key)
        active(sections.get_by_role("tab", name=name, exact=True), name)
        expect(selected_panel).to_contain_text(guidance)
        expect(selected_panel.locator("button,input,select,iframe")).to_have_count(0)
    expect(page.locator("#workspace-manager-status-refresh")).to_be_hidden()

    backup.press("2")
    active(settings_tab, "Settings")
    density = page.get_by_label("Density", exact=True)
    expand = page.get_by_label("Expand workspace details", exact=True)
    density.select_option("compact")
    expand.check()
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(manager).to_have_attribute("data-compact", "")
    page.get_by_role("button", name="Reset to defaults", exact=True).click()
    expect(density).to_have_value("comfortable")
    expect(manager).to_have_attribute("data-compact", "")
    density.press("Escape")
    expect(density).to_have_value("compact")
    expect(expand).to_be_checked()
    settings_tab.press("Home")
    active(status, "Status")
    expect(page.locator("#workspace-manager-details")).to_have_attribute("open", "")
    require(
        selected_panel.locator("td").first.evaluate(
            "cell => getComputedStyle(cell).paddingTop === '5px'"
        ),
        "Compact preference did not affect the rendered manager table.",
    )

    page.set_viewport_size({"width": 390, "height": 800})
    status.press("End")
    active(backup, "Backup/restore")
    require(
        manager.evaluate(
            """el => {
              const tabs = el.querySelectorAll('[role=tab]');
              return el.scrollWidth <= el.clientWidth &&
                tabs[0].offsetTop < tabs[tabs.length - 1].offsetTop &&
                getComputedStyle(el).padding === '12px';
            }"""
        ),
        "Narrow manager did not wrap its tabs or remain within its viewport.",
    )
    backup.press("Home")
    active(status, "Status")
    status.press("Tab")
    expect(selected_panel).to_be_focused()
    page.set_viewport_size({"width": 1440, "height": 900})
    preserved()


def workspace_views(page, gateway, first, history):
    root = page.locator("#web-shell")
    layouts = page.get_by_role("navigation", name="Workspace layout", exact=True)
    frame = page.locator("#native")
    expect(root).to_have_attribute("data-layout", "studio")
    expect(page.locator("#workspace-toolbar .workspace-project")).to_contain_text(
        "synthetic-project"
    )
    expect(page.locator("#workspace-toolbar .workspace-guest")).to_have_text(
        "Workspace guest"
    )
    expect(page.locator("#workspace-review #workspace-layouts")).to_be_visible()
    expect(
        page.locator("#workspace-library").get_by_role("heading", name="Sessions")
    ).to_be_visible()
    expect(
        page.locator("#workspace-overview").get_by_role("heading", name="Status")
    ).to_be_visible()
    require(
        page.evaluate(
            """() => {
              const rect = id => document.getElementById(id).getBoundingClientRect();
              const top = rect("workspace-toolbar"), review = rect("workspace-review");
              const library = rect("workspace-library"), chat = rect("workspace-chat");
              const inspector = rect("workspace-overview");
              return top.height === 68.5 && top.bottom === review.top &&
                review.bottom === library.top && library.top === chat.top && chat.top === inspector.top &&
                library.width === 248 && inspector.width === 278 &&
                chat.left - library.right === 10 && inspector.left - chat.right === 10 &&
                getComputedStyle(document.getElementById("workspace-toolbar")).backgroundColor === "rgb(12, 21, 32)" &&
                document.querySelector(".workspace-brand use").getBBox().width > 0;
            }"""
        ),
        "The final Studio masthead, palette, icons, pane geometry or gutters did not render.",
    )
    expect(page.get_by_role("tab", name="Synthetic Bravo", exact=True)).to_be_visible()
    # Saved membership is real but the fixture supplies no fresh focus report.
    # A bare launcher must open Studio without guessing which tab to select.
    expect(page.locator("#no-selection")).to_be_visible()
    page.get_by_role("tab", name="Synthetic Alpha", exact=True).click()
    expect(page.locator("#workspace-conversation-title")).to_have_text(
        "Synthetic Alpha"
    )
    expect(page.locator("#workspace-conversation-source")).to_have_text(
        "OpenCode / Open TUI tab"
    )
    rendered_session(page, gateway, first, "Synthetic Alpha")
    page.emulate_media(color_scheme="light")
    require(
        frame.evaluate(
            "node => node.contentWindow.matchMedia('(prefers-color-scheme: light)').matches"
        ),
        "Dark Studio chrome changed the native conversation's system theme.",
    )
    # Checking src alone would miss a replacement element or same-URL reload.
    mark = page.evaluate_handle(
        """() => {
          const frame = document.querySelector("#native");
          const nativeDocument = frame.contentDocument;
          const route = frame.contentWindow.location.href;
          const src = frame.getAttribute("src");
          const changed = records => records.some(record =>
            record.type === "attributes" && record.target === frame ||
            [...record.removedNodes].some(node => node.contains(frame))
          );
          let invalidated = false;
          const observer = new MutationObserver(records => { invalidated ||= changed(records); });
          observer.observe(document.body, {
            subtree: true, childList: true, attributes: true, attributeFilter: ["src"]
          });
          return {
            preserved: () => !invalidated && !changed(observer.takeRecords()) &&
              document.querySelectorAll("iframe").length === 1 &&
              document.querySelector("#native") === frame &&
              frame.contentDocument === nativeDocument &&
              frame.contentWindow.location.href === route && frame.getAttribute("src") === src,
            stop: () => observer.disconnect()
          };
        }"""
    )

    def preserved():
        require(
            mark.evaluate("mark => mark.preserved()"),
            "Layout changed the mounted native document or route.",
        )

    def select(label, view):
        button = layouts.get_by_role("button", name=label, exact=True)
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(root).to_have_attribute("data-layout", view)

    requests = []

    def first_party(request):
        if request.frame == page.main_frame:
            requests.append((request.method, urlsplit(request.url).path))

    page.on("request", first_party)
    try:
        divider = page.get_by_role("separator", name="Resize sessions pane")
        divider.press("ArrowRight")
        expect(divider).to_have_attribute("aria-valuenow", "264")
        divider.press("Enter")
        expect(divider).to_have_attribute("aria-valuenow", "248")
        preserved()

        select("Dashboard", "dashboard")
        expect(frame).to_be_visible()
        preserved()

        select("Chat focus", "focus")
        expect(frame).to_be_visible()
        toggle = page.locator("#workspace-sessions-toggle")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        search = page.get_by_role("searchbox", name="Search by title or session ID")
        expect(search).to_be_focused()
        search.press("Escape")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        toggle.click()
        page.get_by_role("button", name="Close sessions", exact=True).click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        preserved()

        select("Studio", "studio")
        expect(frame).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 800})
        inspector = page.get_by_role("button", name="Inspector", exact=True)
        expect(inspector).to_be_visible()
        expect(page.locator("#workspace-library")).to_be_hidden()
        expect(frame).to_be_visible()
        inspector.click()
        overview = page.get_by_role("heading", name="Status", exact=True)
        expect(overview).to_be_focused()
        overview.press("Escape")
        expect(inspector).to_be_focused()
        expect(inspector).to_have_attribute("aria-expanded", "false")
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(inspector).to_be_visible()
        expect(overview).to_be_visible()
        page.get_by_role("button", name="Close inspector", exact=True).click()
        expect(overview).to_be_hidden()
        expect(page.locator("#workspace-divider-right")).to_be_hidden()
        inspector.click()
        expect(overview).to_be_visible()
        expect(page.locator("#workspace-divider-right")).to_be_visible()
        preserved()
        select("Workspace manager", "manager")
        expect(frame).to_be_hidden()
        expect(page.locator("#workspace-content")).to_be_hidden()
        manager_views(page, preserved)
        require(
            not requests, "Viewing manager panels or display settings issued a request."
        )
        page.get_by_role("button", name="Refresh TUI membership").click()
        expect(
            page.get_by_role("button", name="Refresh TUI membership")
        ).to_be_enabled()
        require(
            requests == [("GET", web_sessions.CATALOG)],
            "Explicit Status refresh did not use only the existing TUI catalog.",
        )
        preserved()
        select("Studio", "studio")
        expect(frame).to_be_visible()
        preserved()

        select("Workspace manager", "manager")
        sections = page.get_by_role("tablist", name="Workspace manager sections")
        sections.get_by_role("tab", name="Sessions", exact=True).click()
        preserved()
        require(
            len(requests) == 1, "Entering Sessions automatically requested history."
        )
        source = page.get_by_label("Source", exact=True)
        source.select_option("history")
        expect(page.locator("#workspace-manager-session-count")).to_contain_text(
            "workspace history rows"
        )
        table = page.get_by_role("table", name="Workspace session results")
        expect(
            table.get_by_role(
                "button", name="Open in native chat: Synthetic History-only"
            )
        ).to_be_visible()
        archived = table.get_by_role(
            "button", name="Open in native chat: Synthetic Archived"
        )
        expect(archived).to_be_visible()
        page.get_by_label("Include archived").uncheck()
        expect(archived).to_have_count(0)
        source.select_option("archived")
        expect(archived).to_be_visible()
        expect(table.get_by_role("button")).to_have_count(1)
        source.select_option("history")
        page.get_by_label("Include archived").check()
        search = page.get_by_role("searchbox", name="Search sessions", exact=True)
        search.fill("ses_browser_history")
        expect(table.get_by_role("button")).to_have_count(1)
        expect(
            table.get_by_role(
                "button", name="Open in native chat: Synthetic History-only"
            )
        ).to_be_visible()
        page.get_by_role("button", name="Clear search", exact=True).click()
        expect(search).to_be_focused()
        expect(archived).to_be_visible()
        require(
            requests == [("GET", web_sessions.CATALOG), ("GET", web_sessions.HISTORY)],
            "History filtering or browsing fetched anything beyond the existing catalogs.",
        )
        preserved()
    finally:
        page.remove_listener("request", first_party)
        mark.evaluate("mark => mark.stop()")
        mark.dispose()

    # A deliberate session click differs from changing presentation. It routes
    # the existing iframe to a new native session, returning to the chat view.
    mounted = frame.element_handle()
    page.get_by_role(
        "button", name="Open in native chat: Synthetic History-only", exact=True
    ).click()
    expect(root).to_have_attribute("data-layout", "studio")
    rendered_session(page, gateway, history, "Synthetic History-only")
    require(
        mounted.evaluate("node => node === document.querySelector('#native')"),
        "Explicit selection replaced the iframe.",
    )


def exercise(binary, browser, root):
    project = root / "synthetic-project"
    project.mkdir()
    env = private_environment(root / "native")
    home = Path(env["HOME"])
    for name, relative in (
        ("CONFIG", ".config"),
        ("STATE", ".local/state"),
        ("DATA", ".local/share"),
        ("CACHE", ".cache"),
    ):
        env["XDG_" + name + "_HOME"] = str(home / relative)
    env.update(
        TERM="xterm-256color", COLORTERM="truecolor", OPENCODE_DISABLE_AUTOUPDATE="1"
    )
    config = home / ".config/opencode"
    config.mkdir(parents=True)
    (config / "cli.json").write_text(
        json.dumps(
            {
                "tabs": {"mode": "on", "scope": "cwd", "layout": "horizontal"},
                "animations": False,
                "highlights": False,
            }
        )
    )
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(
        native_tabs.supports_web_tabs(release),
        "This fixture requires stable native OpenCode 2.x.",
    )
    data = {
        "id": "a" * 32,
        "project": str(project),
        "home": str(home),
        "container": "synthetic-no-podman",
        "opencode_version": release,
    }
    original_run = box.run
    original_popen = subprocess.Popen

    def local_popen(command, *args, **settings):
        if (
            isinstance(command, box.ContainerCommand)
            and command[:3] == ["podman", "exec", "-i"]
            and data["container"] in command
        ):
            marker = command.index(data["container"])
            command = command[marker + 1 :]
        return original_popen(command, *args, **settings)

    def local_exec(command, **settings):
        marker = command.index(data["container"])
        require(
            command[:3] == ["podman", "exec", "-i"]
            and command[marker + 1 : marker + 5] == ["python3", "-I", "-S", "-c"],
            "Fixture lost the providerless production environment bridge.",
        )
        if "input" not in settings:
            settings["stdin"] = subprocess.DEVNULL
        return original_run(
            command[marker + 1 :], cwd=project, env=command.environment, **settings
        )

    def local_relay(_box, _data, port):
        require(port == endpoint.port, "Gateway selected an unexpected native relay.")
        return box.ContainerCommand(
            [sys.executable, "-B", "-c", service.PROXY, str(port)], env
        )

    def snapshot():
        return native_tabs.read_snapshot(str(home), str(project), release)

    previous = Path.cwd()
    os.chdir(project)
    try:
        with (
            patch.dict(os.environ, env, clear=True),
            bridge.server(binary, startup_deadline=time.monotonic() + 35) as native,
        ):
            info = native("GET", "/api/info")
            endpoint = urlsplit(native.endpoint["url"])
            require(
                info["version"] == release and endpoint.hostname == "127.0.0.1",
                "Private native service identity mismatch.",
            )
            require(
                native("GET", "/api/credential")["data"] == [],
                "Fixture inherited credentials.",
            )
            first = seed(native, "ses_browser_alpha", project, title="Synthetic Alpha")
            second = seed(native, "ses_browser_bravo", project, title="Synthetic Bravo")
            history = seed(
                native, "ses_browser_history", project, title="Synthetic History-only"
            )
            archived = seed(
                native, "ses_browser_archived", project, title="Synthetic Archived"
            )
            native("PATCH", "/api/session/" + archived, {"archived": True})
            members = (first, second)
            session_ids = {*members, history, archived}
            for expected in ((first,), members):
                client = Client(binary, env, project, native.endpoint, expected[-1])
                try:
                    client.wait(
                        lambda expected=expected: snapshot().tabs == expected,
                        "browser smoke native membership",
                    )
                    client.drain(1.3)
                finally:
                    client.close()
            before_tabs = snapshot()
            before_exports = {ident: exported(native, ident) for ident in session_ids}
            connection = {
                **native.endpoint,
                "workspace": data["id"],
                "session": first,
                "version": release,
            }

            with (
                patch.object(box, "run", side_effect=local_exec),
                patch.object(service, "relay_command", side_effect=local_relay),
                patch.object(
                    web_history.subprocess,
                    "Popen",
                    side_effect=local_popen,
                ),
            ):
                launch_browser(
                    browser,
                    data,
                    connection,
                    [],
                    lambda page, gateway: workspace_views(
                        page, gateway, first, history
                    ),
                )
                print(
                    f"PASS: Chromium {browser.version}, Studio through the launcher's "
                    "real one-use link (including replay refusal).",
                    flush=True,
                )

            require(
                snapshot() == before_tabs,
                "Browser interaction changed native TUI membership.",
            )
            require(
                {info["id"] for info in bridge.service_session_catalog(native)}
                == session_ids
                and all(
                    exported(native, ident) == before_exports[ident]
                    for ident in before_exports
                ),
                "Browser interaction created or changed native history.",
            )
            print(
                f"PASS: native {release}, one mounted document across all workspace views; explicit session selection and unchanged native history.",
                flush=True,
            )
    finally:
        os.chdir(previous)


def executable(value):
    path = Path(value).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError("provide a path to an executable file")
    return str(path)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--opencode",
        required=True,
        type=executable,
        metavar="PATH",
        help="unmodified stable native OpenCode V2 executable",
    )
    parser.add_argument(
        "--chromium",
        required=True,
        type=executable,
        metavar="PATH",
        help="Chromium or Chrome Headless Shell executable",
    )
    args = parser.parse_args()
    if sync_playwright is None:
        parser.error("Playwright is required; see the invocation in --help")

    with tempfile.TemporaryDirectory(prefix="oc-box-web-layout-browser-") as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in private_environment(root / "browser").items()
            if not key.startswith("OPENCODE_")
        }
        if "PLAYWRIGHT_NODEJS_PATH" in os.environ:
            env["PLAYWRIGHT_NODEJS_PATH"] = os.environ["PLAYWRIGHT_NODEJS_PATH"]
        with patch.dict(os.environ, env, clear=True), sync_playwright() as automation:
            browser = automation.chromium.launch(
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
                exercise(args.opencode, browser, root)
            finally:
                browser.close()
    print("Offline synthetic browser regression only; NOT live host/Podman acceptance.")


if __name__ == "__main__":
    main()
