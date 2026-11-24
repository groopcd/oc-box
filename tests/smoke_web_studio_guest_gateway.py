"""Verify the private managed one-use gateway with real Chromium and native 2.4.1.

Run non-root in a disposable, loopback-only network namespace. The native
process, browser, wire, original listener and relay pipes are real. Outer
container inspect/exec and the one-turn, no-tool provider are synthetic.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from oc_box import (
    opencode_v2_io,
    web_guest_duplex_exec,
    web_studio_guest,
    web_studio_guest_gateway,
    web_studio_guest_ingress,
)
from named_browser_send_fixture import Provider, REPLY_TEXT, SEND_TEXT
from smoke_named_pake_browser import browser_home, launch, loopback_namespace
from smoke_session_sync_v2 import FixtureFailure, require, seed
from smoke_web_studio_guest_owner import _NATIVE_SHA, digest, exercise


_SESSION = "ses_managed_guest_fixture"
_TITLE = "One-use Gateway fixture"


def verify(owner, project, browser):
    original_exec = subprocess.Popen
    phase = "native-setup"
    navigation = []
    setup = opencode_v2_io.endpoint_request(owner.connection)

    def safe_seed(method, path, value):
        require(
            method == "POST" and path == "/api/experimental/session/import",
            "Unexpected native fixture setup call.",
        )
        value["info"]["permissions"] = [
            {"action": "tool.use", "resource": "*", "effect": "deny"}
        ]
        return setup(method, path, value)

    seed(safe_seed, _SESSION, project, title=_TITLE)
    container_id = owner._guest_proof.current().container_id
    spawned = []

    def substitute(command, **kw):
        require(
            command[:14]
            == [
                "podman",
                "exec",
                "-i",
                "--workdir",
                "/",
                container_id,
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-B",
                "-c",
            ]
            and "OPENCODE_PASSWORD" not in kw["env"]
            and "OPENCODE_CONFIG_CONTENT" not in kw["env"]
            and SEND_TEXT not in " ".join(command),
            "Unexpected private relay command.",
        )
        peer = original_exec([sys.executable, *command[10:]], **kw)
        spawned.append(peer)
        return peer

    class OuterExec:
        Popen = staticmethod(substitute)

        def __getattr__(self, name):
            return getattr(subprocess, name)

    result = False
    gateway = web_studio_guest_gateway._ManagedGateway(owner._guest_proof, studio=True)
    try:
        with patch.object(web_guest_duplex_exec, "subprocess", OuterExec()):
            phase = "gateway"
            require(gateway.start(), "Private owning Gateway could not start.")
            link = setup("POST", "/api/pair")["code"]
            require(
                re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", link),
                "Native pairing code was invalid.",
            )
            origin = gateway.url
            pair_url = origin + "/auth/connect/" + link
            with browser.new_context(service_workers="block") as context:
                context.set_default_timeout(10000)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda _: errors.append(True))

                def classified(url):
                    if url == pair_url:
                        return "pair"
                    if url == origin + web_studio_guest.PAGE_PATH:
                        return "document"
                    if url.startswith(origin + "/oc-box/studio/"):
                        return "studio"
                    return "other"

                def network_failure(request):
                    code = request.failure
                    if (
                        type(code) is not str
                        or re.fullmatch(r"net::ERR_[A-Z_]{1,48}", code) is None
                    ):
                        code = "failure"
                    if len(navigation) < 20:
                        navigation.append((classified(request.url), code))

                def network_reply(reply):
                    if len(navigation) < 20:
                        navigation.append((classified(reply.url), reply.status))

                page.on("requestfailed", network_failure)
                page.on("response", network_reply)
                phase = "native-pairing"
                response = page.goto(pair_url, wait_until="load")
                require(
                    response.status == 200
                    and page.url == origin + web_studio_guest.PAGE_PATH
                    and not page.locator("iframe").count(),
                    "Native pairing did not open the first-party document.",
                )
                phase = "guest-confirmation"
                confirm = page.get_by_role(
                    "button", name="Confirm this browser", exact=True
                )
                with page.expect_response(
                    lambda reply: reply.url == origin + web_studio_guest.CONFIRM_PATH
                ) as proof:
                    confirm.click()
                require(
                    proof.value.status == 200,
                    "The original native cookie did not confirm.",
                )
                phase = "catalog-selection-transcript"
                page.get_by_role("tab", name=_TITLE, exact=True).click()
                center = page.locator("#workspace-studio-center")
                expect(
                    center.get_by_text("SYNTHETIC_" + _SESSION, exact=True)
                ).to_be_visible(timeout=10000)
                phase = "send"
                field = center.get_by_role("textbox", name="Message", exact=True)
                field.fill(SEND_TEXT)
                with page.expect_response(
                    lambda reply: reply.url == origin + web_studio_guest_ingress.SEND
                ) as sent:
                    center.get_by_role("button", name="Send", exact=True).click()
                require(
                    sent.value.status == 200
                    and sent.value.json() == {"status": "queued"},
                    "The native queue receipt could not be verified.",
                )
                phase = "retained-conversation"
                expect(center.get_by_text(REPLY_TEXT, exact=True)).to_be_visible(
                    timeout=10000
                )
                expect(center.get_by_text(SEND_TEXT, exact=True)).to_have_count(1)
                require(not errors, "The first-party browser script failed.")
                phase = "pairing-replay"
                replay = page.request.get(pair_url, max_redirects=0)
                require(
                    replay.status == 401, "Native one-use link replay was not refused."
                )
            phase = "drain"
            require(
                gateway.drain(),
                "Private browser worker or owner drain was not verified.",
            )
            require(
                spawned
                and all(peer.returncode == 0 for peer in spawned)
                and owner.child.poll() is None,
                "Not all exact relays retired before release to the fixture owner.",
            )
            result = True
    except FixtureFailure:
        raise
    except Exception as error:
        require(
            False,
            f"Private managed browser fixture refused during {phase} ({type(error).__name__}; "
            f"relays={len(spawned)}; fixed network categories={navigation}).",
        )
    finally:
        if not result and not gateway.drain():
            print(
                "Private managed browser fixture retirement was not verified.",
                file=sys.stderr,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    args = parser.parse_args()
    try:
        loopback_namespace()
        binary = args.opencode.resolve(strict=True)
        require(digest(binary) == _NATIVE_SHA, "Unexpected native artifact.")
        chrome = args.chromium.resolve(strict=True)
        with TemporaryDirectory(prefix="ocb-studio-managed-gateway-") as directory:
            root = Path(directory)
            home = browser_home(root, "browser")
            with sync_playwright() as pw, Provider(_SESSION).serving() as provider:
                browser = launch(pw, chrome, home, ("127.0.0.1",))
                try:
                    exercise(
                        root,
                        binary,
                        configure=provider.config,
                        test_gateway=lambda owner, project: verify(
                            owner, project, browser
                        ),
                    )
                finally:
                    browser.close()
    except Exception as error:
        print(
            str(error)
            if type(error) is FixtureFailure
            else f"Private managed gateway fixture refused ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 1
    print(
        "PASS private Chromium and OpenCode 2.4.1: original pair, one Send, exact replay refusal and drain."
    )
    print(
        "Synthetic outer container inspect/exec and provider; no installed or Podman acceptance."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
