"""Real reserved HTTPS with pinned 2.4.1; synthetic owner and authenticated grant."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from traceback import extract_tb
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError, sync_playwright

from oc_box import (
    oc_box,
    web_accounts,
    web_local_origins,
    web_named_http,
    web_named_listeners,
    web_named_owner,
    web_named_relay,
    web_named_site_lifecycle,
    web_pake_flow,
    web_pake_profile,
    web_studio_conversation,
    workspace_service,
)
from named_browser_native_fixture import WorkerCommand, native_fixture
from smoke_named_bootstrap_browser import material, require
from smoke_named_pake_browser import RESEARCH, browser_home, launch, loopback_namespace
from smoke_session_sync_v2 import seed


_SESSION = "ses_named_studio_fixture"
_TITLE = "Named Studio browser fixture"
_NEIGHBOR = "ses_named_studio_send_other"


def launcher(native):
    class Refused(ValueError):
        pass

    def client_command(box, data, connection, argv):
        require(
            data["project"] == str(native.project)
            and connection["url"] == native.endpoint
            and argv[:5] == ["python3", "-I", "-S", "-B", "-c"]
            and argv[-4:]
            == [
                web_studio_conversation._MANAGED_ARG,
                native.endpoint,
                str(native.project),
                _SESSION,
            ],
            "The synthetic exec was not the exact scoped transcript worker.",
        )
        return WorkerCommand(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "import os,sys; os.chdir(sys.argv[1]); os.execv(sys.argv[2], sys.argv[2:])",
                str(native.project),
                sys.executable,
                *argv[1:],
            ],
            connection["password"],
        )

    return SimpleNamespace(
        BoxError=Refused,
        ContainerCommand=oc_box.ContainerCommand,
        maintenance_host_env=lambda: {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        container_project=lambda data: data["project"],
        workspace_service=SimpleNamespace(
            validate_endpoint=workspace_service.validate_endpoint,
            client_command=client_command,
        ),
    )


def exercise(path, chrome, binary, evidence, *, prove_send=False):
    research = runpy.run_path(str(RESEARCH / "check_browser"))
    with chrome.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == research["CHROME_SHA"],
            "Pinned Chromium executable mismatch.",
        )
    hosts = {
        "account": web_local_origins.ACCOUNT_HOST,
        "workspace": web_local_origins.WORKSPACE_HOST,
    }
    ca, pairs = material(path, hosts)
    home = browser_home(path, "trusted", ca)
    with ExitStack() as cleanup:
        configure = proof = after_stop = None
        if prove_send:
            from named_browser_send_fixture import Provider, SendProof

            provider = cleanup.enter_context(Provider(_SESSION).serving())
            proof = SendProof(provider, _SESSION)
            configure = provider.config
            after_stop = lambda request: proof.verify_history(request, wait=False)
        native = cleanup.enter_context(
            native_fixture(
                path,
                binary,
                version="2.4.1",
                configure=configure,
                after_stop=after_stop,
            )
        )
        if proof is None:
            seed(native.request, _SESSION, native.project, title=_TITLE)
        else:

            def seed_without_tools(method, target, value):
                value["info"]["permissions"] = [
                    {"action": "tool.use", "resource": "*", "effect": "deny"}
                ]
                return native.request(method, target, value, deadline=monotonic() + 4)

            seed(seed_without_tools, _SESSION, native.project, title=_TITLE)
            seed(
                seed_without_tools, _NEIGHBOR, native.project, title="Untouched fixture"
            )
            neighbor_route = "/api/experimental/session/" + _NEIGHBOR + "/export"
            neighbor_before = native.request(
                "GET", neighbor_route, deadline=monotonic() + 4
            )
        profile = cleanup.enter_context(web_named_listeners.reserve_profile(**pairs))
        control, control_peer = map(cleanup.enter_context, socket.socketpair())
        identity = web_named_owner.OwnerIdentity(
            "a" * 32,
            "b" * 32,
            "c" * 64,
            native.version,
            native.endpoint,
        )
        box = launcher(native)
        port = str(urlsplit(native.endpoint).port)
        original_start = web_named_relay._WorkspaceChild.start
        workers = []

        def connect(owner, *, track, check, deadline):
            require(owner is identity, "The fixture selected a different native owner.")
            check()
            track.start_workspace_relay(
                box, container_id=identity.container_id, endpoint=identity.endpoint
            )
            check()

        def exact_local_exec(child, command, stdio):
            if proof is not None:
                workers.append(child)
            if command[0] != "podman":
                return original_start(child, command, stdio)
            require(
                command
                == [
                    "podman",
                    "exec",
                    "-i",
                    "--workdir",
                    "/",
                    identity.container_id,
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "python3",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    workspace_service.PROXY,
                    port,
                ]
                and "OPENCODE_PASSWORD" not in command.environment,
                "The browser fixture received an unexpected managed relay command.",
            )
            local = box.ContainerCommand(
                [sys.executable, *command[-6:]], command.environment
            )
            return original_start(child, local, stdio)

        lease = web_named_owner.VerifiedOwnerLease(
            identity,
            control,
            str(native.project),
            str(native.home),
            native.request.endpoint["password"],
            lambda: identity if native.request.alive() else None,
            connect,
        )
        backend = Mock(sizes=web_pake_flow.MessageSizes(96, 320, 64))
        backend.close.return_value = True
        with patch.object(web_pake_profile, "LocalPakeProfile", return_value=backend):
            sites = web_named_site_lifecycle.prepare_sites(
                box,
                profile=profile,
                lease=lease,
                store=web_accounts.AccountStore(b"s" * 128, ()),
                account_current=lambda: True,
                verifier_fd=7,
                expected_sha256=b"d" * 32,
            )
        try:
            cleanup.enter_context(
                patch.object(web_named_relay._WorkspaceChild, "start", exact_local_exec)
            )
            origin = profile.origins.workspace
            binding = sites.services._owner.binding
            _, pair = native.bind(binding, origin)
            cookie = pair(binding, origin)
            admission = sites.services.application.sign_in.admission
            token = admission.issue(
                binding,
                principal="synthetic",
                native_cookie=cookie,
            )
            require(token is not None, "Fixture app grant refused.")
            if proof is not None:
                session = admission.lookup(binding, token)
                require(session is not None, "Fixture app grant was not current.")
                cleanup.enter_context(proof.observe(identity, session))
            web_named_http.start_named_http(
                sites,
                profile,
                login_document=b"<h1>Unused synthetic account page</h1>",
                expires_at=monotonic() + 40,
                studio=True,
                studio_ui=True,
            )
            with sync_playwright() as pw:
                browser = launch(pw, chrome, home, hosts.values())
                try:
                    with browser.new_context(
                        viewport={"width": 2269, "height": 1398}
                    ) as context:
                        context.add_cookies(
                            [
                                {
                                    "name": "__Host-ocbox-app",
                                    "value": token,
                                    "url": origin.value + "/",
                                    "secure": True,
                                    "httpOnly": True,
                                    "sameSite": "Strict",
                                }
                            ]
                        )
                        require(
                            any(
                                item["name"] == "__Host-ocbox-app"
                                for item in context.cookies()
                            ),
                            "Chromium did not retain the synthetic host-only app grant.",
                        )
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        routes = {
                            "/": "document",
                            "/oc-box/studio/catalog": "catalog",
                            "/oc-box/studio/selection": "selection",
                            "/oc-box/studio/conversation": "transcript",
                            "/oc-box/studio/web_sessions.js": "shell-script",
                            "/oc-box/studio/web_named_studio_entry.mjs": "entry-module",
                            "/oc-box/studio/web_named_studio_client.mjs": "client-module",
                            "/oc-box/studio/web_studio_shell.css": "shell-css",
                        }
                        if proof is not None:
                            routes["/oc-box/studio/send"] = "send"

                        def observe(url, outcome):
                            parsed = urlsplit(url)
                            if (
                                f"{parsed.scheme}://{parsed.netloc}" == origin.value
                                and parsed.path in routes
                            ):
                                evidence[routes[parsed.path]] = outcome

                        page.on(
                            "response",
                            lambda response: observe(response.url, response.status),
                        )
                        page.on(
                            "requestfailed",
                            lambda request: observe(request.url, "no-response"),
                        )
                        response = page.goto(origin.value + "/")
                        require(
                            response is not None and response.ok,
                            "Named Studio landing failed.",
                        )
                        require(
                            page.locator("iframe").count() == 0
                            and page.evaluate("document.cookie === ''"),
                            "Named Studio exposed an iframe or credential.",
                        )
                        row = page.get_by_role("tab", name=_TITLE, exact=True)
                        row.wait_for()
                        with page.expect_response(
                            lambda r: r.url.endswith("/oc-box/studio/selection")
                        ) as selected:
                            row.click()
                        require(
                            selected.value.status == 200,
                            "Native named selection was not accepted.",
                        )
                        page.get_by_text("SYNTHETIC_" + _SESSION, exact=True).wait_for()
                        require(
                            page.get_by_role(
                                "button", name="Send", exact=True
                            ).is_disabled(),
                            "An empty draft enabled Send.",
                        )
                        if proof is not None:
                            send_from_composer(page, origin, proof)
                            proof.verify_history(native.request)
                            require(
                                native.request(
                                    "GET", neighbor_route, deadline=monotonic() + 4
                                )
                                == neighbor_before,
                                "The browser Send changed a different fixture session.",
                            )
                finally:
                    browser.close()
        finally:
            require(
                sites.close(timeout=10), "Named Studio sites or app uses did not drain."
            )
        require(
            control.fileno() >= 0,
            "The site unexpectedly closed the synthetic owner lease.",
        )
        require(
            native.request("GET", "/api/session/active", deadline=monotonic() + 4)
            == {"data": {}},
            "The Studio fixture left an active native agent.",
        )
        if proof is not None:
            require(
                workers
                and all(
                    child.confirmed
                    and child._exit == 0
                    and child._pidfd is None
                    and child._child.returncode == 0
                    for child in workers
                )
                and provider.requests == provider.completed == 1
                and provider.rejection is None,
                "The isolated browser Send left a relay or provider outcome unconfirmed.",
            )


def send_from_composer(page, origin, proof):
    from named_browser_send_fixture import SEND_TEXT

    url = origin.value + "/oc-box/studio/send"
    sends = []

    def observe(request):
        if request.url == url:
            sends.append(request.method)

    page.on("request", observe)
    require(proof.provider.requests == 0, "Inference began before the browser Send.")
    editor = page.get_by_role("textbox", name="Message", exact=True)
    button = page.get_by_role("button", name="Send", exact=True)
    editor.click()
    page.keyboard.insert_text(SEND_TEXT)
    require(
        editor.input_value() == SEND_TEXT, "The browser did not enter the fixture text."
    )
    with page.expect_response(lambda response: response.url == url) as reply:
        button.click()
    require(
        reply.value.status == 200 and reply.value.body() == b'{"status":"queued"}',
        "The browser did not receive the exact native queue acknowledgment.",
    )
    page.get_by_text(
        "OpenCode acknowledged the queued message. Execution is not confirmed.",
        exact=True,
    ).wait_for()
    require(
        editor.input_value() == "" and button.is_disabled() and sends == ["POST"],
        "The composer did not clear the one acknowledged Send.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument(
        "--prove-send",
        action="store_true",
        help="Also prove one explicit browser Send with a fixed local no-tool provider.",
    )
    args = parser.parse_args()
    loopback_namespace()
    os.umask(0o077)
    with TemporaryDirectory(prefix="ocbox-named-studio-https-") as root:
        evidence = {}
        try:
            exercise(
                Path(root),
                args.chromium.resolve(strict=True),
                args.opencode.resolve(strict=True),
                evidence,
                prove_send=args.prove_send,
            )
        except BrowserError as error:
            location = next(
                (
                    frame.lineno
                    for frame in reversed(extract_tb(error.__traceback__))
                    if frame.filename == __file__
                ),
                None,
            )
            raise RuntimeError(
                f"Named Studio Chromium flow did not complete (line {location}; {json.dumps(evidence, sort_keys=True)})."
            ) from None
    print(
        "PASS Chromium and OpenCode 2.4.1: trusted named Studio landing, catalog, selection, and transcript."
    )
    if args.prove_send:
        print(
            "PASS one authentic browser Send: native resume:true, queued acknowledgment, "
            "exact synthetic provider turn, text retained after reopen, confirmed cleanup."
        )
        print(
            "Synthetic owner and app grant, local exec substitution and fixed provider; "
            "no login or Podman acceptance."
        )
    else:
        print(
            "Synthetic owner and app grant; no login proof, Send, provider, or Podman acceptance."
        )


if __name__ == "__main__":
    main()
