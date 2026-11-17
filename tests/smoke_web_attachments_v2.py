"""Provider-free native web adapter/API contract; not a browser or host test.

python3 -B tests/smoke_web_attachments_v2.py --opencode /path/to/opencode-v2

Only fresh private synthetic state is used. Standard native pairing cookies,
bundled app assets and API media admission remain unmodified. Outer Podman exec
is replaced with a local pipe relay. A synthetic chooser catalog contains real
native API sessions; actual TUI membership belongs to smoke_web_sessions_v2.py.
The HTTP client checks response-close policy, not browser rendering/file drops.
Deliberate resume=false fixture admissions
below test native PNG/PDF handling; the production adapter has no prompt call.
"""

import argparse
import base64
import contextlib
from html import unescape
from http.client import HTTPConnection
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import (
    oc_box as box,
    opencode_compat,
    opencode_v2_io as bridge,
    web_attachments,
    web_sessions,
    workspace_service,
)
from smoke_session_sync_v2 import bounded, private_environment, require, seed
from smoke_sharing_v2 import PNG


def shell_state(body):
    match = re.search(rb'data-bootstrap="([^"]*)"', body)
    require(match is not None, "Common shell bootstrap is missing.")
    require(body.count(b"<iframe ") == 1, "Attachment addon introduced another iframe.")
    require(
        body.count(web_attachments.SCRIPT.encode()) == 1,
        "Trusted addon script slot is missing or duplicated.",
    )
    return json.loads(unescape(match[1].decode()))


def pdf():
    """Small valid synthetic one-page PDF with an exact cross-reference table."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = []
    for index, content in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(index).encode() + b" 0 obj\n" + content + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 5\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return (
        result
        + f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )


def exercise(binary, root):
    env = private_environment(root / "private")
    project = root / "synthetic-project"
    project.mkdir()
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(
        release in web_attachments.SUPPORTED,
        "Only native 2.2.6/2.3.0 are source-bound.",
    )
    previous = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True), bridge.server(binary) as native:
            require(
                native("GET", "/api/info")["version"] == release,
                "Native identity mismatch.",
            )
            require(
                native("GET", "/api/credential")["data"] == [],
                "Fixture inherited credentials.",
            )
            ident = seed(native, "ses_web_attachments", project)
            other = seed(native, "ses_web_other", project)
            members = [ident, other]
            catalog = [
                native("GET", "/api/session/" + selected)["data"]
                for selected in members
            ]
            before = {
                selected: native(
                    "GET", "/api/experimental/session/" + selected + "/export"
                )
                for selected in members
            }
            inbox = {
                selected: native("GET", "/api/session/" + selected + "/inbox")
                for selected in members
            }
            connection = {**native.endpoint, "version": release, "session": None}

            def local_relay(_box, _data, port):
                return box.ContainerCommand(
                    [sys.executable, "-B", "-c", workspace_service.PROXY, str(port)],
                    env,
                )

            with (
                patch.object(
                    workspace_service, "relay_command", side_effect=local_relay
                ),
                patch.object(
                    web_sessions, "open_sessions", return_value=(catalog, "valid")
                ),
                workspace_service.Gateway(
                    box,
                    {},
                    connection,
                    attachments=True,
                    shell_addon_scripts=(web_attachments.SCRIPT,),
                ) as gateway,
            ):
                browser = build_opener(
                    ProxyHandler({}), HTTPCookieProcessor(CookieJar())
                )
                try:
                    browser.open(gateway.url + web_attachments.SCRIPT, timeout=10)
                except HTTPError as error:
                    require(error.code == 401, "Unpaired adapter did not refuse.")
                else:
                    raise AssertionError("Unpaired adapter was accepted.")
                pair = native("POST", "/api/pair")
                with browser.open(
                    Request(
                        gateway.url + "/auth/connect/" + pair["code"],
                        headers={"Accept": "text/html"},
                    ),
                    timeout=10,
                ) as reply:
                    require(
                        reply.geturl() == gateway.url + web_sessions.INDEX,
                        "Pairing did not open the common shell.",
                    )
                    chooser = reply.read()
                    require(
                        b"Open native OpenCode Home" in chooser,
                        "Native Home navigation missing.",
                    )
                    require(
                        shell_state(chooser)["selection"] is None,
                        "Unbound shell invented an owner/first-row selection.",
                    )
                for selected in members:
                    link = web_sessions.INDEX + "?session=" + selected
                    require(
                        gateway.native_session_route(selected).encode() in chooser,
                        "Native fallback row missing.",
                    )
                    with browser.open(gateway.url + link, timeout=10) as reply:
                        body = reply.read()
                    state = shell_state(body)
                    require(
                        state["selection"]["id"] == selected,
                        "Shell selected the wrong conversation.",
                    )
                    require(
                        (
                            ' src="' + gateway.native_session_route(selected) + '"'
                        ).encode()
                        in body,
                        "Common iframe did not target the selected native session.",
                    )
                    require(
                        re.search(rb'<iframe\b[^>]*\bsrc="/auth/', body) is None,
                        "Common shell incorrectly framed itself.",
                    )
                with browser.open(
                    gateway.url + web_attachments.SCRIPT, timeout=10
                ) as reply:
                    require(b"DataTransfer" in reply.read(), "Adapter script missing.")
                with browser.open(
                    gateway.url + gateway.native_session_route(ident), timeout=10
                ) as reply:
                    html = reply.read().decode()
                    asset = re.search(r'<script[^>]*\bsrc="(/_assets/[^"]+\.js)"', html)
                    require(
                        asset is not None, "Bundled native app entry is unavailable."
                    )
                with browser.open(Request(gateway.url + asset[1]), timeout=10) as reply:
                    require(
                        reply.status == 200
                        and "javascript" in reply.headers["Content-Type"]
                        and len(reply.read(128)) > 0,
                        "Bundled native JavaScript is unavailable.",
                    )
                try:
                    browser.open(
                        Request(
                            gateway.url + web_attachments.SCRIPT,
                            headers={"Origin": "https://outside.invalid"},
                        ),
                        timeout=10,
                    )
                except HTTPError as error:
                    require(error.code == 403, "Foreign-origin addon did not refuse.")
                else:
                    raise AssertionError("Foreign-origin addon was accepted.")
                try:
                    browser.open(
                        gateway.url + "/auth/oc-box-attachments?session=" + ident,
                        timeout=10,
                    )
                except HTTPError as error:
                    require(
                        error.code == 404,
                        "Retired independent wrapper route did not disappear.",
                    )
                else:
                    raise AssertionError("Retired independent wrapper still exists.")

                # This client requests persistent connections, unlike urllib.
                # Native final-response headers must force a fresh transaction
                # before each host-dispatched shell/addon resource.
                client = HTTPConnection(
                    "127.0.0.1", gateway.listener.getsockname()[1], timeout=15
                )
                try:
                    pair = native("POST", "/api/pair")
                    headers = {"Accept": "text/html", "Connection": "keep-alive"}
                    client.request(
                        "GET", "/auth/connect/" + pair["code"], headers=headers
                    )
                    reply = client.getresponse()
                    require(
                        reply.status == 302
                        and reply.getheader("Location") == web_sessions.INDEX,
                        "Keep-alive pairing did not select the common shell.",
                    )
                    cookies = SimpleCookie()
                    for name, value in reply.getheaders():
                        if name.lower() == "set-cookie":
                            cookies.load(value)
                    headers["Cookie"] = "; ".join(
                        item.OutputString(attrs=[]) for item in cookies.values()
                    )
                    require(
                        bool(headers["Cookie"]) and reply.will_close,
                        "Keep-alive pairing lost cookies or allowed socket reuse.",
                    )
                    reply.read()
                    for route in (
                        web_sessions.INDEX,
                        gateway.native_session_route(ident),
                        web_sessions.INDEX + "?session=" + ident,
                        gateway.native_session_route(other),
                        web_sessions.INDEX + "?session=" + other,
                        web_attachments.SCRIPT,
                        web_sessions.INDEX,
                    ):
                        client.request("GET", route, headers=headers)
                        reply = client.getresponse()
                        body = reply.read()
                        require(
                            reply.status == 200
                            and reply.will_close
                            and reply.getheader("Connection", "").lower() == "close",
                            "Native-to-first-party keep-alive navigation lost dispatch.",
                        )
                        if route.startswith(web_sessions.INDEX + "?"):
                            selected = parse_qs(urlsplit(route).query)["session"][0]
                            state = shell_state(body)
                            require(
                                state["selection"]["id"] == selected,
                                "Keep-alive shell selected the wrong ID.",
                            )
                            require(
                                (
                                    ' src="'
                                    + gateway.native_session_route(selected)
                                    + '"'
                                ).encode()
                                in body,
                                "Keep-alive common iframe selected the wrong session.",
                            )
                finally:
                    client.close()

            require(
                all(
                    native("GET", "/api/experimental/session/" + selected + "/export")
                    == before[selected]
                    for selected in members
                ),
                "Wrapper reads changed retained history.",
            )
            require(
                all(
                    native("GET", "/api/session/" + selected + "/inbox")
                    == inbox[selected]
                    for selected in members
                ),
                "Wrapper reads admitted or queued user input.",
            )
            print(
                "PASS native "
                + release
                + ": common shell/two-row catalog, single selected iframe and addon slot, authenticated assets, native/host keep-alive close-policy navigation, foreign-origin refusal, retired wrapper 404, unchanged history/inboxes."
            )

            # Explicit, separate fixture mutation, never a production drop action.
            media_session = seed(native, "ses_web_media_admission", project)
            pdf_data = base64.b64encode(pdf()).decode()
            admitted = native(
                "POST",
                "/api/session/" + media_session + "/prompt",
                {
                    "text": "SYNTHETIC_MEDIA_ADMISSION_ONLY",
                    "files": [
                        {
                            "uri": "data:image/png;base64," + PNG,
                            "name": "synthetic.png",
                        },
                        {
                            "uri": "data:application/pdf;base64," + pdf_data,
                            "name": "synthetic.pdf",
                        },
                    ],
                    "resume": False,
                },
            )["data"]
            require(
                [file["mime"] for file in admitted["payload"]["files"]]
                == ["image/png", "application/pdf"],
                "Native PNG/PDF admission mismatch.",
            )
            require(
                native("GET", "/api/session/active")["data"] == {},
                "Provider-free admission started execution.",
            )
            require(
                len(native("GET", "/api/session/" + media_session + "/inbox")["data"])
                == 1,
                "Expected explicit fixture-only pending admission.",
            )
            print(
                "PASS native "
                + release
                + ": explicit fixture-only PNG/PDF bytes admitted with resume=false; no inference."
            )
    finally:
        os.chdir(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oc-box-web-attachments-") as directory:
        exercise(str(args.opencode.resolve()), Path(directory))
    print(
        "Native/API plus separate synthetic DOM tests only; NOT live browser, Spectacle, provider or rootless-Podman acceptance."
    )


if __name__ == "__main__":
    main()
