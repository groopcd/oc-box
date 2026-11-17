"""Provider-free native web adapter/API contract; not a browser or host test.

python3 -B tests/smoke_web_attachments_v2.py --opencode /path/to/opencode-v2

Only fresh private synthetic state is used. Standard native pairing cookies,
bundled app assets and API media admission remain unmodified. Outer Podman exec
is replaced with a local pipe relay. A synthetic chooser catalog contains real
native API sessions; actual TUI membership belongs to smoke_web_sessions_v2.py.
The HTTP client does not test normal browser keep-alive or rendering.
Deliberate resume=false fixture admissions
below test native PNG/PDF handling; the production adapter has no prompt call.
"""

import argparse
import base64
import contextlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError
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
                    box, {}, connection, attachments=True
                ) as gateway,
            ):
                browser = build_opener(
                    ProxyHandler({}), HTTPCookieProcessor(CookieJar())
                )
                try:
                    browser.open(
                        gateway.url + web_attachments.session_link(ident), timeout=10
                    )
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
                        "Pairing did not open the chooser.",
                    )
                    chooser = reply.read()
                    require(
                        b"Open native OpenCode Home" in chooser,
                        "Native Home navigation missing.",
                    )
                for selected in members:
                    link = web_attachments.session_link(selected)
                    require(link.encode() in chooser, "Selected wrapper row missing.")
                    with browser.open(gateway.url + link, timeout=10) as reply:
                        body = reply.read()
                    require(
                        b"oc-box attachment adapter" in body, "Adapter page missing."
                    )
                    require(
                        (
                            'data-route="'
                            + gateway.native_session_route(selected)
                            + '"'
                        ).encode()
                        in body,
                        "Wrapper iframe did not target the selected native session.",
                    )
                    require(
                        b'data-route="' + web_sessions.INDEX.encode() not in body,
                        "Wrapper incorrectly framed the chooser.",
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
                            gateway.url + web_attachments.session_link(ident),
                            headers={"Origin": "https://outside.invalid"},
                        ),
                        timeout=10,
                    )
                except HTTPError as error:
                    require(error.code == 403, "Foreign-origin wrapper did not refuse.")
                else:
                    raise AssertionError("Foreign-origin wrapper was accepted.")

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
                + ": synthetic two-row chooser, selected wrappers/native iframe routes, authenticated assets, foreign-origin refusal, unchanged history/inboxes."
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
